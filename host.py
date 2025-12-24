import logging
import os
import socket
from os import environ
from typing import Type

from aiohttp.web import Application, Request, Response, json_response, run_app
from aiohttp.web_middlewares import middleware as web_middleware
from microsoft_agents_a365.observability.core.config import configure
from microsoft_agents.activity import load_configuration_from_env
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
from pydantic import BaseModel, Field

from agent import AgentInterface
from microsoft_agents_a365.observability.core.middleware.baggage_builder import (
    BaggageBuilder,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class HostConfig(BaseModel):
    port: int = Field(default=3978)
    host: str = Field(default="localhost")
    service_name: str = Field(default="agent365-service")
    service_namespace: str = Field(default="agent-monitoring")
    enable_observability: bool = Field(default=True)

    @classmethod
    def from_env(cls) -> "HostConfig":
        return cls(
            port=int(os.getenv("PORT", "3978")),
            service_name=os.getenv("OBSERVABILITY_SERVICE_NAME", "agent365-service"),
            service_namespace=os.getenv("OBSERVABILITY_SERVICE_NAMESPACE", "agent-monitoring"),
            enable_observability=os.getenv("ENABLE_OBSERVABILITY", "true").lower() == "true",
        )


class AgentHost:
    def __init__(self, agent_class: Type[AgentInterface], config: HostConfig = None):
        self.agent_class = agent_class
        self.agent_instance = None
        self.config = config or HostConfig.from_env()
        self.auth_handler_name = "AGENTIC"

        if self.config.enable_observability:
            configure(
                service_name=self.config.service_name,
                service_namespace=self.config.service_namespace,
            )

        self.storage = MemoryStorage()
        self.agents_sdk_config = load_configuration_from_env(environ)
        self.connection_manager = MsalConnectionManager(**self.agents_sdk_config)
        self.adapter = CloudAdapter(connection_manager=self.connection_manager)
        self.authorization = Authorization(
            self.storage, self.connection_manager, **self.agents_sdk_config
        )
        self.agent_app = AgentApplication[TurnState](
            storage=self.storage,
            adapter=self.adapter,
            authorization=self.authorization,
            **self.agents_sdk_config,
        )

        # Register auth handler early if credentials are available
        self.auth_config = self.create_auth_configuration()
        if self.auth_config:
            self.agent_app.auth.add(
                self.auth_handler_name,
                self.auth_config,
            )
            logger.info(f"✅ Registered authentication handler: {self.auth_handler_name}")
        else:
            logger.warning("⚠️ No authentication - running in anonymous mode")
        
        self._setup_handlers()

    def _setup_handlers(self):
        """Setup message handlers with or without authentication."""
        # Only require auth handlers if authentication is configured
        handler = [self.auth_handler_name] if self.auth_config else []

        async def help_handler(context: TurnContext, _: TurnState):
            await context.send_activity(
                f"👋 **Hi there!** I'm **{self.agent_class.__name__}**, your AI assistant.\n\n"
                "How can I help you today?"
            )

        if handler:
            self.agent_app.conversation_update("membersAdded", auth_handlers=handler)(help_handler)
            self.agent_app.message("/help", auth_handlers=handler)(help_handler)

            @self.agent_app.activity("message", auth_handlers=handler)
            async def on_message(context: TurnContext, _: TurnState):
                await self._process_message(context, _)
        else:
            # Anonymous mode - no auth required
            self.agent_app.conversation_update("membersAdded")(help_handler)
            self.agent_app.message("/help")(help_handler)

            @self.agent_app.activity("message")
            async def on_message(context: TurnContext, _: TurnState):
                await self._process_message(context, _)

    async def _process_message(self, context: TurnContext, _: TurnState):
        try:
            result = await self._validate_agent_and_setup_context(context)
            if result is None:
                return
            tenant_id, agent_id = result

            user_message = context.activity.text or ""
            logger.info(f"📨 Processing message: {user_message[:50]}...")
            
            if not user_message.strip() or user_message.strip() == "/help":
                return

            with BaggageBuilder().tenant_id(tenant_id).agent_id(agent_id).build():
                response = await self.agent_instance.process_user_message(
                    user_message, self.authorization, self.auth_handler_name, context
                )
                logger.info(f"✅ Sending response: {response[:50]}...")
                await context.send_activity(response)
        except Exception as e:
            logger.error(f"❌ Error processing message: {str(e)}", exc_info=True)
            await context.send_activity(f"Sorry, I encountered an error: {str(e)}")

    async def initialize_agent(self):
        if self.agent_instance is None:
            self.agent_instance = self.agent_class()
            await self.agent_instance.initialize()

    async def cleanup(self):
        if self.agent_instance:
            await self.agent_instance.cleanup()

    def create_auth_configuration(self) -> AgentAuthConfiguration | None:
        """Create authentication configuration from environment variables."""
        client_id = environ.get("CLIENT_ID")
        tenant_id = environ.get("TENANT_ID")
        client_secret = environ.get("CLIENT_SECRET")

        if client_id and tenant_id and client_secret:
            logger.info("🔒 Using Client Credentials authentication")
            return AgentAuthConfiguration(
                client_id=client_id,
                tenant_id=tenant_id,
                client_secret=client_secret,
                scopes=["5a807f24-c9de-44ee-a3a7-329e88a00ffc/.default"],
            )

        if environ.get("BEARER_TOKEN"):
            logger.info("🔑 Anonymous dev mode with bearer token")
        else:
            logger.warning("⚠️ No auth env vars; running anonymous mode")
        return None

    async def _setup_observability_token(
        self, context: TurnContext, tenant_id: str, agent_id: str
    ):
        """Setup observability token for tracing"""
        try:
            from microsoft_agents_a365.runtime.environment_utils import (
                get_observability_authentication_scope,
            )
            from auth import cache_agentic_token
            
            exaau_token = await self.agent_app.auth.exchange_token(
                context,
                scopes=get_observability_authentication_scope(),
                auth_handler_id=self.auth_handler_name,
            )
            cache_agentic_token(tenant_id, agent_id, exaau_token.token)
        except Exception as e:
            logger.debug(f"Failed to cache observability token: {e}")

    async def _validate_agent_and_setup_context(self, context: TurnContext):
        tenant_id = getattr(context.activity.recipient, "tenant_id", "")
        agent_id = getattr(context.activity.recipient, "agentic_app_id", "")
        
        logger.info(f"🔍 Validating context - tenant_id: {tenant_id or 'N/A'}, agent_id: {agent_id or 'N/A'}")

        if not self.agent_instance:
            logger.error("❌ Agent instance not available")
            await context.send_activity("❌ Sorry, the agent is not available.")
            return None
        
        # Setup observability token if auth is configured
        if tenant_id and agent_id and self.auth_config:
            await self._setup_observability_token(context, tenant_id, agent_id)
        
        return tenant_id, agent_id

    def run(self, port: int = None):
        port = port or self.config.port

        async def entry_point(req: Request) -> Response:
            return await start_agent_process(
                req, req.app["agent_app"], req.app["adapter"]
            )

        async def health(_req: Request) -> Response:
            return json_response(
                {
                    "status": "ok",
                    "agent": self.agent_class.__name__,
                    "initialized": self.agent_instance is not None,
                }
            )

        # Auth configuration already set in __init__
        auth_configuration = self.auth_config
        
        middlewares = []
        
        # Add request logging middleware
        @web_middleware
        async def request_logger(request, handler):
            logger.info(f"📥 Incoming request: {request.method} {request.path}")
            if request.method == "POST":
                try:
                    # Read body for logging (be careful with large payloads)
                    body = await request.text()
                    logger.debug(f"Request body: {body[:200]}...")
                except Exception:
                    pass
            response = await handler(request)
            logger.info(f"📤 Response status: {response.status}")
            return response
        
        middlewares.append(request_logger)
        
        if auth_configuration:
            middlewares.append(jwt_authorization_middleware)

        @web_middleware
        async def anonymous_claims(request, handler):
            if not auth_configuration:
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

        app["agent_app"] = self.agent_app
        app["adapter"] = self.agent_app.adapter

        app.on_startup.append(lambda app: self.initialize_agent())
        app.on_shutdown.append(lambda app: self.cleanup())

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex((self.config.host, port)) == 0:
                port += 1

        print("=" * 80)
        print(f"{self.agent_class.__name__}")
        print("=" * 80)
        print(f"Server: {self.config.host}:{port}")
        print(f"Endpoint: http://{self.config.host}:{port}/api/messages")
        print(f"Health: http://{self.config.host}:{port}/api/health\n")

        run_app(app, host=self.config.host, port=port, handle_signals=True)
