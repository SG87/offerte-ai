"""FastAPI application: web app + REST API (/api/v1) + MCP server (/mcp) in one process.

uvicorn app.main:app --reload
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.sessions import SessionMiddleware

from app import __version__
from app.api.routes import router as api_router
from app.config import get_settings
from app.db import init_db
from app.mcp_server import mcp
from app.web.account import router as account_router
from app.web.agent import router as agent_router
from app.web.beheer import router as beheer_router
from app.web.facturen import router as facturen_router
from app.web.feedback import router as feedback_router
from app.web.klant import router as klant_router
from app.web.leverancier import router as leverancier_router
from app.web.routes import router as web_router

# the MCP transport is one ASGI route; we add it to the main router so POST /mcp answers directly
# (a Mount at /mcp would 307-redirect to /mcp/, which not every MCP client follows)
mcp_app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=get_settings().mcp_allowed_hosts_list,
        allowed_origins=[f"http://{h}" for h in get_settings().mcp_allowed_hosts_list]
        + [f"https://{h}" for h in get_settings().mcp_allowed_hosts_list],
    ),
)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    async with mcp.session_manager.run():
        yield


app = FastAPI(
    title="Offerte-AI",
    version=__version__,
    description="AI-native offerteplatform voor vakmensen. REST API onder /api/v1, MCP-server onder /mcp.",
    lifespan=lifespan,
    docs_url="/docs",
)
app.add_middleware(SessionMiddleware, secret_key=get_settings().secret_key, same_site="lax")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "web" / "static")), name="static")
app.include_router(api_router, prefix="/api/v1")
app.include_router(web_router)
app.include_router(beheer_router)
app.include_router(facturen_router)
app.include_router(feedback_router)
app.include_router(account_router)
app.include_router(agent_router)
app.include_router(klant_router)
app.include_router(leverancier_router)
app.router.routes.extend(mcp_app.routes)


@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok", "version": __version__, "ai_enabled": get_settings().ai_enabled}


def cli() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
