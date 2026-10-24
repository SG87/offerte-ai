"""Application settings, loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT_DIR / "skills"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Anthropic
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-sonnet-5-5", alias="ANTHROPIC_MODEL")

    # App
    secret_key: str = Field(default="dev-secret-change-me", alias="OFFERTE_SECRET_KEY")
    database_url: str = Field(default="sqlite:///./data/offerte.db", alias="OFFERTE_DATABASE_URL")
    data_dir: Path = Field(default=Path("./data"), alias="OFFERTE_DATA_DIR")
    base_url: str = Field(default="http://localhost:8000", alias="OFFERTE_BASE_URL")

    # MCP (v1 dev mode): organisation the MCP server acts for
    mcp_org_email: str | None = Field(default=None, alias="OFFERTE_MCP_ORG_EMAIL")
    # Host headers the HTTP MCP transport accepts (DNS-rebinding protection). Add your tunnel hostname
    # (e.g. "abc.trycloudflare.com") when exposing /mcp to the Claude app. "host:*" allows any port.
    mcp_allowed_hosts: str = Field(
        default="localhost:*,127.0.0.1:*,localhost,127.0.0.1", alias="OFFERTE_MCP_ALLOWED_HOSTS"
    )

    @property
    def mcp_allowed_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.mcp_allowed_hosts.split(",") if h.strip()]

    @property
    def ai_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def outputs_dir(self) -> Path:
        return self.data_dir / "outputs"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.uploads_dir.mkdir(parents=True, exist_ok=True)
    s.outputs_dir.mkdir(parents=True, exist_ok=True)
    return s
