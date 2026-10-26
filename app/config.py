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

    # Outgoing mail (purchase lists). Without SMTP_HOST mails wait in the outbox (integrations are placeholders in v1).
    smtp_host: str | None = Field(default=None, alias="OFFERTE_SMTP_HOST")
    smtp_port: int = Field(default=587, alias="OFFERTE_SMTP_PORT")
    smtp_user: str | None = Field(default=None, alias="OFFERTE_SMTP_USER")
    smtp_password: str | None = Field(default=None, alias="OFFERTE_SMTP_PASSWORD")
    smtp_from: str | None = Field(default=None, alias="OFFERTE_SMTP_FROM")
    mail_from_name: str = Field(default="Offer AI", alias="OFFERTE_MAIL_FROM_NAME")  # display name in From
    # Test mode: every mail goes to this address instead of the real recipient (suppliers etc. get nothing)
    mail_redirect_to: str | None = Field(default=None, alias="OFFERTE_MAIL_REDIRECT_TO")
    # Triggers for the stock agent / planning tool: POSTed here as JSON when set (HMAC-signed with the secret)
    webhook_url: str | None = Field(default=None, alias="OFFERTE_WEBHOOK_URL")
    webhook_secret: str | None = Field(default=None, alias="OFFERTE_WEBHOOK_SECRET")
    # Peppol: the access point's API that takes a UBL invoice (POST, application/xml, Bearer key). Without it,
    # approved B2B invoices are 'klaar voor Peppol' and the UBL can be downloaded and uploaded elsewhere.
    peppol_api_url: str | None = Field(default=None, alias="OFFERTE_PEPPOL_API_URL")
    peppol_api_key: str | None = Field(default=None, alias="OFFERTE_PEPPOL_API_KEY")
    # Distance to the work site (kilometre allowance): OpenStreetMap geocoding + OSRM routing. Off = typed by hand.
    geo_enabled: bool = Field(default=True, alias="OFFERTE_GEO_ENABLED")
    geocoder_url: str = Field(default="https://nominatim.openstreetmap.org/search", alias="OFFERTE_GEOCODER_URL")
    router_url: str = Field(default="https://router.project-osrm.org", alias="OFFERTE_ROUTER_URL")

    @property
    def mcp_allowed_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.mcp_allowed_hosts.split(",") if h.strip()]

    @property
    def smtp_ready(self) -> bool:
        """A host, and a password when a user is set (Gmail/Workspace: an app password)."""
        return bool(self.smtp_host) and (not self.smtp_user or bool(self.smtp_password))

    @property
    def peppol_ready(self) -> bool:
        return bool(self.peppol_api_url)

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
