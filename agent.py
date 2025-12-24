import json
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from agent_framework import ChatAgent
from agent_framework.azure import AzureOpenAIChatClient
from azure.identity import AzureCliCredential
from dotenv import load_dotenv
from microsoft_agents_a365.observability.extensions.agentframework.trace_instrumentor import AgentFrameworkInstrumentor
from microsoft_agents_a365.tooling.extensions.agentframework.services.mcp_tool_registration_service import McpToolRegistrationService
from pydantic import BaseModel, Field

from auth import AuthConfig

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class McpServer(BaseModel):
    mcpServerName: str = Field(...)
    mcpServerUniqueName: str = Field(...)
    url: str = Field(...)
    scope: str = Field(...)
    audience: str = Field(...)


class ToolingManifest(BaseModel):
    mcpServers: list[McpServer] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path = None) -> "ToolingManifest":
        if path is None:
            path = Path("ToolingManifest.json")
        
        if not path.exists():
            logger.warning(f"ToolingManifest.json not found at {path}")
            return cls(mcpServers=[])
        
        with open(path, "r") as f:
            data = json.load(f)
        return cls(**data)


class AgentConfig(BaseModel):
    endpoint: str = Field(...)
    deployment: str = Field(...)
    api_version: str = Field(default="2024-02-15-preview")
    instructions: str = Field(default="You are a helpful assistant.")
    enable_observability: bool = Field(default=True)
    enable_otel: bool = Field(default=True)
    enable_a365_observability_exporter: bool = Field(default=False)
    enable_sensitive_data: bool = Field(default=True)
    enable_mcp: bool = Field(default=False)
    mcp_server_host: str = Field(default="")
    mcp_platform_endpoint: str = Field(default="")
    python_environment: str = Field(default="development")
    tooling_manifest: Optional[ToolingManifest] = Field(default=None)

    @classmethod
    def from_env(cls) -> "AgentConfig":
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "")
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "")
        if not endpoint or not deployment:
            raise ValueError("AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT required")
        
        enable_mcp = os.getenv("ENABLE_MCP", "false").lower() == "true"
        manifest = ToolingManifest.load() if enable_mcp else None
        
        return cls(
            endpoint=endpoint,
            deployment=deployment,
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview"),
            enable_observability=os.getenv("ENABLE_OBSERVABILITY", "true").lower() == "true",
            enable_otel=os.getenv("ENABLE_OTEL", "true").lower() == "true",
            enable_a365_observability_exporter=os.getenv("ENABLE_A365_OBSERVABILITY_EXPORTER", "false").lower() == "true",
            enable_sensitive_data=os.getenv("ENABLE_SENSITIVE_DATA", "true").lower() == "true",
            enable_mcp=enable_mcp,
            mcp_server_host=os.getenv("MCP_SERVER_HOST", ""),
            mcp_platform_endpoint=os.getenv("MCP_PLATFORM_ENDPOINT", ""),
            python_environment=os.getenv("PYTHON_ENVIRONMENT", "development"),
            tooling_manifest=manifest,
        )


class AgentInterface(ABC):
    @abstractmethod
    async def initialize(self) -> None:
        pass

    @abstractmethod
    async def process_user_message(self, message: str, auth, auth_handler_name: str, context) -> str:
        pass

    @abstractmethod
    async def cleanup(self) -> None:
        pass


class Agent365Agent(AgentInterface):
    def __init__(self, config: Optional[AgentConfig] = None, auth: Optional[AuthConfig] = None):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.config = config or AgentConfig.from_env()
        self.auth = auth or AuthConfig.from_env()
        self.agent: Optional[ChatAgent] = None
        self.tool_service: Optional[McpToolRegistrationService] = None
        self.mcp_initialized = False
        
        if self.config.enable_observability:
            AgentFrameworkInstrumentor().instrument()
            self.logger.info(f"✅ Observability enabled (OTEL: {self.config.enable_otel}, A365 Exporter: {self.config.enable_a365_observability_exporter})")

    async def initialize(self) -> None:
        self.chat_client = AzureOpenAIChatClient(
            endpoint=self.config.endpoint,
            credential=AzureCliCredential(),
            deployment_name=self.config.deployment,
            api_version=self.config.api_version,
        )
        
        self.agent = ChatAgent(
            chat_client=self.chat_client,
            instructions=self.config.instructions,
            tools=[],
        )
        
        if self.config.enable_mcp:
            self.tool_service = McpToolRegistrationService()
            if self.config.tooling_manifest:
                server_count = len(self.config.tooling_manifest.mcpServers)
                self.logger.info(f"✅ MCP tool service initialized ({server_count} servers)")
            else:
                self.logger.info("✅ MCP tool service initialized (no servers)")
        
        self.logger.info(f"✅ {self.__class__.__name__} initialized")

    async def _setup_mcp(self, auth_context: dict):
        if not self.config.enable_mcp or self.mcp_initialized or not self.tool_service:
            return
        
        if self.auth.use_agentic_auth:
            self.logger.info("Using agentic authentication for MCP")
            self.agent = await self.tool_service.add_tool_servers_to_agent(
                chat_client=self.chat_client,
                agent_instructions=self.config.instructions,
                initial_tools=[],
            )
        else:
            self.logger.info("Using bearer token authentication for MCP")
            self.agent = await self.tool_service.add_tool_servers_to_agent(
                chat_client=self.chat_client,
                agent_instructions=self.config.instructions,
                initial_tools=[],
                auth_token=self.auth.bearer_token,
            )
        self.mcp_initialized = True
        self.logger.info("✅ MCP servers configured")

    async def process_message(self, message: str, context: dict) -> str:
        if not self.agent:
            return "Agent not initialized"
        
        await self._setup_mcp(context)
        
        self.logger.info(f"📨 Processing: {message}")
        result = await self.agent.run(message)
        
        if hasattr(result, "contents"):
            return str(result.contents)
        return str(result)

    async def process_user_message(self, message: str, auth, auth_handler_name: str, context) -> str:
        # Minimal wrapper to align with SDK handler signature
        return await self.process_message(message, {"auth": auth, "handler": auth_handler_name, "context": context})

    async def cleanup(self) -> None:
        if self.tool_service:
            await self.tool_service.cleanup()
        self.logger.info("Agent cleanup completed")
