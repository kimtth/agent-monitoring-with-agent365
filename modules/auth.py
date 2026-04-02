import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class LocalAuthenticationOptions:
    """
    Authentication options for local/dev scenarios (bearer token MCP access).
    For production service principal auth, the host reads CONNECTIONS__ vars directly.
    """
    env_id: str = ""
    bearer_token: str = ""

    @property
    def is_valid(self) -> bool:
        return bool(self.env_id and self.bearer_token)

    @classmethod
    def from_environment(cls) -> "LocalAuthenticationOptions":
        return cls(
            env_id=os.getenv("ENV_ID", ""),
            bearer_token=os.getenv("BEARER_TOKEN", ""),
        )
