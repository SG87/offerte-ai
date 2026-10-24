"""Test setup: isolated SQLite DB and data dir per session, no API key (AI paths degrade gracefully)."""

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="offerte-ai-test-")
os.environ["OFFERTE_DATA_DIR"] = _tmp
os.environ["OFFERTE_DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ["OFFERTE_SECRET_KEY"] = "test-secret"
os.environ["OFFERTE_MCP_ALLOWED_HOSTS"] = "testserver,localhost:*,127.0.0.1:*"
