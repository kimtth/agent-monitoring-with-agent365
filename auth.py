import os
import logging

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()
logger = logging.getLogger(__name__)

# Global token cache for Agent 365 Observability exporter
_agentic_token_cache = {}


def cache_agentic_token(tenant_id: str, agent_id: str, token: str) -> None:
    """Cache the agentic token for use by Agent 365 Observability exporter."""
    key = f"{tenant_id}:{agent_id}"
    _agentic_token_cache[key] = token
    logger.debug(f"Cached agentic token for {key}")


def get_cached_agentic_token(tenant_id: str, agent_id: str) -> str | None:
    """Retrieve cached agentic token for Agent 365 Observability exporter."""
    key = f"{tenant_id}:{agent_id}"
    token = _agentic_token_cache.get(key)
    if token:
        logger.debug(f"Retrieved cached agentic token for {key}")
    else:
        logger.debug(f"No cached token found for {key}")
    return token


class AuthConfig(BaseModel):
    env_id: str = Field(default="")
    bearer_token: str = Field(default="")
    client_id: str = Field(default="")
    client_secret: str = Field(default="")
    tenant_id: str = Field(default="")
    scopes: list[str] = Field(default_factory=lambda: ["5a807f24-c9de-44ee-a3a7-329e88a00ffc/.default"])
    use_agentic_auth: bool = Field(default=True)

    @classmethod
    def from_env(cls) -> "AuthConfig":
        # Support both direct env vars and CONNECTIONS__ prefixed vars
        client_id = os.getenv("CLIENT_ID") or os.getenv("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID", "")
        client_secret = os.getenv("CLIENT_SECRET") or os.getenv("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET", "")
        tenant_id = os.getenv("TENANT_ID") or os.getenv("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID", "")
        scopes_str = os.getenv("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__SCOPES", "")
        scopes = scopes_str.split(",") if scopes_str else ["5a807f24-c9de-44ee-a3a7-329e88a00ffc/.default"]
        
        return cls(
            env_id=os.getenv("ENV_ID", ""),
            bearer_token=os.getenv("BEARER_TOKEN", ""),
            client_id=client_id,
            client_secret=client_secret,
            tenant_id=tenant_id,
            scopes=scopes,
            use_agentic_auth=os.getenv("USE_AGENTIC_AUTH", "true").lower() == "true",
        )

    class Config:
        frozen = False
