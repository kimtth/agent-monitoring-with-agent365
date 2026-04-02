import asyncio
import logging
import os
import socket
from os import environ
from typing import Type

from aiohttp.web import Application, Request, Response, json_response, run_app
from aiohttp.web_middlewares import middleware as web_middleware
from dotenv import load_dotenv
from microsoft_agents_a365.observability.core.config import configure
from microsoft_agents.activity import load_configuration_from_env, Activity
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import CloudAdapter, start_agent_process, jwt_authorization_middleware
from microsoft_agents.hosting.core import (
    AgentApplication,
    AgentAuthConfiguration,
    AuthenticationConstants,
    Authorization,
    ClaimsIdentity,
    MemoryStorage,
    TurnContext,
    TurnState,
)
from microsoft_agents_a365.notifications.agent_notification import (
    AgentNotification,
    AgentNotificationActivity,
    ChannelId,
    NotificationTypes,
)
from microsoft_agents_a365.notifications import EmailResponse
from microsoft_agents_a365.observability.core.middleware.baggage_builder import BaggageBuilder
from microsoft_agents_a365.runtime.environment_utils import get_observability_authentication_scope
from pydantic import BaseModel, Field

from .agent import AgentInterface
from .token_cache import cache_agentic_token

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

agents_sdk_config = load_configuration_from_env(environ)


class HostConfig(BaseModel):
    port: int = Field(default=3978)
    host: str = Field(default="localhost")
    service_name: str = Field(default="agent365-service")
    service_namespace: str = Field(default="agent-monitoring")
    enable_observability: bool = Field(default=True)

    @classmethod
    def from_env(cls) -> "HostConfig":
        # Bind to 0.0.0.0 when running on App Service so health probes can reach the container
        is_app_service = bool(os.getenv("WEBSITE_SITE_NAME"))
        return cls(
            port=int(os.getenv("PORT", "3978")),
            host="0.0.0.0" if is_app_service else "localhost",
            service_name=os.getenv("OBSERVABILITY_SERVICE_NAME", "agent365-service"),
            service_namespace=os.getenv("OBSERVABILITY_SERVICE_NAMESPACE", "agent-monitoring"),
            enable_observability=os.getenv("ENABLE_OBSERVABILITY", "true").lower() == "true",
        )


class AgentHost:
    def __init__(self, agent_class: Type[AgentInterface], config: HostConfig = None):
        self.agent_class = agent_class
        self.agent_instance = None
        self.config = config or HostConfig.from_env()

        # AUTH_HANDLER_NAME=AGENTIC for production; empty/unset = anonymous mode
        self.auth_handler_name = os.getenv("AUTH_HANDLER_NAME", "") or None
        if self.auth_handler_name:
            logger.info(f"🔐 Using auth handler: {self.auth_handler_name}")
        else:
            logger.info("🔓 No auth handler configured — running in anonymous mode")

        if self.config.enable_observability:
            configure(
                service_name=self.config.service_name,
                service_namespace=self.config.service_namespace,
            )

        self.storage = MemoryStorage()
        self.connection_manager = MsalConnectionManager(**agents_sdk_config)
        self.adapter = CloudAdapter(connection_manager=self.connection_manager)
        self.authorization = Authorization(
            self.storage, self.connection_manager, **agents_sdk_config
        )
        self.agent_app = AgentApplication[TurnState](
            storage=self.storage,
            adapter=self.adapter,
            authorization=self.authorization,
            **agents_sdk_config,
        )
        self.agent_notification = AgentNotification(self.agent_app)

        self._setup_handlers()
        logger.info("✅ Notification handlers registered")

    # -------------------------------------------------------------------------
    # Handlers
    # -------------------------------------------------------------------------

    def _setup_handlers(self):
        handler_config = {"auth_handlers": [self.auth_handler_name]} if self.auth_handler_name else {}

        async def help_handler(context: TurnContext, _: TurnState):
            await context.send_activity(
                f"👋 **Hi there!** I'm **{self.agent_class.__name__}**, your AI assistant.\n\n"
                "How can I help you today?"
            )

        self.agent_app.conversation_update("membersAdded", **handler_config)(help_handler)
        self.agent_app.message("/help", **handler_config)(help_handler)

        @self.agent_app.activity("installationUpdate")
        async def on_installation_update(context: TurnContext, _: TurnState):
            action = context.activity.action
            from_prop = context.activity.from_property
            logger.info(
                "InstallationUpdate — Action: '%s', DisplayName: '%s'",
                action or "(none)",
                getattr(from_prop, "name", "(unknown)") if from_prop else "(unknown)",
            )
            if action == "add":
                await context.send_activity("Thank you for adding me! Looking forward to helping you.")
            elif action == "remove":
                await context.send_activity("Thank you for your time!")

        @self.agent_app.activity("message", **handler_config)
        async def on_message(context: TurnContext, _: TurnState):
            try:
                result = await self._validate_agent_and_setup_context(context)
                if result is None:
                    return
                tenant_id, agent_id = result

                with BaggageBuilder().tenant_id(tenant_id).agent_id(agent_id).build():
                    user_message = context.activity.text or ""
                    if not user_message.strip() or user_message.strip() == "/help":
                        return

                    logger.info(f"📨 {user_message}")
                    await context.send_activity("Got it — working on it…")
                    await context.send_activity(Activity(type="typing"))

                    async def _typing_loop():
                        try:
                            while True:
                                await asyncio.sleep(4)
                                await context.send_activity(Activity(type="typing"))
                        except asyncio.CancelledError:
                            pass

                    typing_task = asyncio.create_task(_typing_loop())
                    try:
                        response = await self.agent_instance.process_user_message(
                            user_message, self.agent_app.auth, self.auth_handler_name, context
                        )
                        await context.send_activity(response)
                    finally:
                        typing_task.cancel()
                        try:
                            await typing_task
                        except asyncio.CancelledError:
                            pass

            except Exception as e:
                logger.error(f"❌ Error: {e}", exc_info=True)
                await context.send_activity(f"Sorry, I encountered an error: {str(e)}")

        @self.agent_notification.on_agent_notification(
            channel_id=ChannelId(channel="agents", sub_channel="*"),
            **handler_config,
        )
        async def on_notification(
            context: TurnContext,
            state: TurnState,
            notification_activity: AgentNotificationActivity,
        ):
            try:
                result = await self._validate_agent_and_setup_context(context)
                if result is None:
                    return
                tenant_id, agent_id = result

                with BaggageBuilder().tenant_id(tenant_id).agent_id(agent_id).build():
                    logger.info(f"📬 {notification_activity.notification_type}")

                    if not hasattr(self.agent_instance, "handle_agent_notification_activity"):
                        logger.warning("⚠️ Agent doesn't support notifications")
                        await context.send_activity("This agent doesn't support notification handling yet.")
                        return

                    response = await self.agent_instance.handle_agent_notification_activity(
                        notification_activity, self.agent_app.auth, self.auth_handler_name, context
                    )

                    if notification_activity.notification_type == NotificationTypes.EMAIL_NOTIFICATION:
                        await context.send_activity(EmailResponse.create_email_response_activity(response))
                        return

                    await context.send_activity(response)

            except Exception as e:
                logger.error(f"❌ Notification error: {e}")
                await context.send_activity(f"Sorry, I encountered an error processing the notification: {str(e)}")

    # -------------------------------------------------------------------------
    # Agent lifecycle
    # -------------------------------------------------------------------------

    async def initialize_agent(self):
        if self.agent_instance is None:
            logger.info(f"🤖 Initializing {self.agent_class.__name__}...")
            self.agent_instance = self.agent_class()
            await self.agent_instance.initialize()

    async def cleanup(self):
        if self.agent_instance:
            await self.agent_instance.cleanup()

    # -------------------------------------------------------------------------
    # Observability token
    # -------------------------------------------------------------------------

    async def _setup_observability_token(self, context: TurnContext, tenant_id: str, agent_id: str):
        if not self.auth_handler_name:
            logger.debug("Skipping observability token exchange (no auth handler)")
            return
        try:
            logger.info(f"🔐 Token exchange for observability (tenant={tenant_id}, agent={agent_id})")
            exaau_token = await self.agent_app.auth.exchange_token(
                context,
                scopes=get_observability_authentication_scope(),
                auth_handler_id=self.auth_handler_name,
            )
            cache_agentic_token(tenant_id, agent_id, exaau_token.token)
            logger.info("✅ Observability token cached")
        except Exception as e:
            logger.warning(f"⚠️ Failed to cache observability token: {e}")

    async def _validate_agent_and_setup_context(self, context: TurnContext):
        tenant_id = context.activity.recipient.tenant_id
        agent_id = context.activity.recipient.agentic_app_id
        logger.info(f"🔍 tenant_id={tenant_id}, agent_id={agent_id}")

        if not self.agent_instance:
            logger.error("❌ Agent instance not available")
            await context.send_activity("❌ Sorry, the agent is not available.")
            return None

        await self._setup_observability_token(context, tenant_id, agent_id)
        return tenant_id, agent_id

    # -------------------------------------------------------------------------
    # Authentication
    # -------------------------------------------------------------------------

    def create_auth_configuration(self) -> AgentAuthConfiguration | None:
        # Primary: explicit CLIENT_ID / TENANT_ID / CLIENT_SECRET
        client_id = environ.get("CLIENT_ID")
        tenant_id = environ.get("TENANT_ID")
        client_secret = environ.get("CLIENT_SECRET")

        # Fallback: CONNECTIONS__SERVICE_CONNECTION__SETTINGS__* (a365 tooling convention)
        if not client_id:
            client_id = environ.get("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID")
        if not tenant_id:
            tenant_id = environ.get("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID")
        if not client_secret:
            client_secret = environ.get("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET")

        if client_id and tenant_id and client_secret:
            logger.info("🔒 Using Client Credentials authentication (client_id=%s)", client_id)
            return AgentAuthConfiguration(
                client_id=client_id,
                tenant_id=tenant_id,
                client_secret=client_secret,
                scopes=["5a807f24-c9de-44ee-a3a7-329e88a00ffc/.default"],
            )

        logger.warning("⚠️ No auth env vars found; running anonymous")
        return None

    # -------------------------------------------------------------------------
    # Server
    # -------------------------------------------------------------------------

    def run(self, port: int = None):
        port = port or self.config.port

        async def entry_point(req: Request) -> Response:
            return await start_agent_process(req, req.app["agent_app"], req.app["adapter"])

        async def health(_req: Request) -> Response:
            return json_response({
                "status": "ok",
                "agent": self.agent_class.__name__,
                "initialized": self.agent_instance is not None,
            })

        middlewares = []

        auth_config = self.create_auth_configuration()

        if auth_config and self.auth_handler_name:
            @web_middleware
            async def jwt_with_health_bypass(request, handler):
                if request.path == "/api/health":
                    return await handler(request)
                return await jwt_authorization_middleware(request, handler)
            middlewares.append(jwt_with_health_bypass)

        @web_middleware
        async def anonymous_claims(request, handler):
            if not (auth_config and self.auth_handler_name):
                request["claims_identity"] = ClaimsIdentity(
                    {
                        AuthenticationConstants.AUDIENCE_CLAIM: "anonymous",
                        AuthenticationConstants.APP_ID_CLAIM: "anonymous-app",
                    },
                    False,
                    "Anonymous",
                )
            return await handler(request)

        middlewares.append(anonymous_claims)
        app = Application(middlewares=middlewares)

        app.router.add_post("/api/messages", entry_point)
        app.router.add_get("/api/messages", lambda _: Response(status=200))
        app.router.add_get("/api/health", health)

        app["agent_configuration"] = auth_config
        app["agent_app"] = self.agent_app
        app["adapter"] = self.agent_app.adapter

        app.on_startup.append(lambda app: self.initialize_agent())
        app.on_shutdown.append(lambda app: self.cleanup())

        desired_port = port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex((self.config.host, desired_port)) == 0:
                port = desired_port + 1

        print("=" * 80)
        print(f"🏢 {self.agent_class.__name__}")
        print("=" * 80)
        print(f"🔒 Auth: {'Enabled (' + self.auth_handler_name + ')' if self.auth_handler_name else 'Anonymous'}")
        print(f"🚀 Server: {self.config.host}:{port}")
        print(f"📚 Endpoint: http://{self.config.host}:{port}/api/messages")
        print(f"❤️  Health:   http://{self.config.host}:{port}/api/health\n")

        try:
            run_app(app, host=self.config.host, port=port, handle_signals=True)
        except KeyboardInterrupt:
            print("\n👋 Server stopped")
