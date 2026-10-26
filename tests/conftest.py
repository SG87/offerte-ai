"""Test setup: isolated SQLite DB and data dir per session, no API key (AI paths degrade gracefully)."""

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="offerte-ai-test-")
os.environ["OFFERTE_DATA_DIR"] = _tmp
os.environ["OFFERTE_DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["ANTHROPIC_API_KEY"] = ""  # overrides a key in .env
for _var in ("OFFERTE_SMTP_HOST", "OFFERTE_SMTP_USER", "OFFERTE_SMTP_PASSWORD", "OFFERTE_SMTP_FROM",
             "OFFERTE_MAIL_REDIRECT_TO", "OFFERTE_WEBHOOK_URL", "OFFERTE_WEBHOOK_SECRET"):
    os.environ[_var] = ""  # never send real mail or webhooks from tests
os.environ["OFFERTE_GEO_ENABLED"] = "false"  # no geocoding/routing calls from tests
os.environ["OFFERTE_SECRET_KEY"] = "test-secret"
os.environ["OFFERTE_MCP_ALLOWED_HOSTS"] = "testserver,localhost:*,127.0.0.1:*"


import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def client():
    """One app client per run: the MCP session manager in the app lifespan can only start once per process."""
    from app.main import app

    with TestClient(app) as c:
        yield c
