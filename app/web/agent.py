"""JSON routes for the AI agent pane (right side of every page)."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app.ai import agent
from app.ai.client import AIUnavailable
from app.config import get_settings
from app.db import get_session
from app.models import AgentAction, Organisation
from app.web.routes import current_org

router = APIRouter(include_in_schema=False)


def _no_org():
    return JSONResponse({"error": "Meld je eerst aan."}, status_code=401)


@router.post("/agent/vraag")
def agent_ask(
    body: dict = Body(...), org: Organisation | None = Depends(current_org), session: Session = Depends(get_session)
):
    """Sync on purpose: the agent call takes a while and runs in FastAPI's thread pool, not on the event loop."""
    if not org:
        return _no_org()
    question = (body.get("message") or "").strip()
    if not question:
        return JSONResponse({"error": "Stel een vraag."}, status_code=400)
    if not get_settings().ai_enabled:
        return JSONResponse({"error": "AI staat uit: zet ANTHROPIC_API_KEY in .env."}, status_code=503)
    conv = agent.get_conversation(session, org, body.get("conversation_id")) or agent.new_conversation(session, org)
    try:
        out = agent.ask(session, org, conv, question)
    except AIUnavailable as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    except Exception as exc:  # API errors: show them in the pane instead of a blank failure
        return JSONResponse({"error": f"De AI gaf een fout: {type(exc).__name__}: {exc}", "conversation_id": conv.id}, status_code=502)
    return {"conversation_id": conv.id, **out}


@router.get("/agent/gesprek/{conversation_id}")
def agent_history(conversation_id: int, org: Organisation | None = Depends(current_org), session: Session = Depends(get_session)):
    if not org:
        return _no_org()
    conv = agent.get_conversation(session, org, conversation_id)
    if not conv:
        return JSONResponse({"error": "Gesprek niet gevonden"}, status_code=404)
    return agent.history(session, conv)


def _action(session: Session, org: Organisation, action_id: int) -> AgentAction | None:
    a = session.get(AgentAction, action_id)
    return a if a and a.org_id == org.id else None


@router.post("/agent/acties/{action_id}/uitvoeren")
def agent_run(action_id: int, org: Organisation | None = Depends(current_org), session: Session = Depends(get_session)):
    if not org:
        return _no_org()
    a = _action(session, org, action_id)
    if not a:
        return JSONResponse({"error": "Actie niet gevonden"}, status_code=404)
    return agent.action_out(agent.run_action(session, org, a))


@router.post("/agent/acties/{action_id}/annuleren")
def agent_cancel(action_id: int, org: Organisation | None = Depends(current_org), session: Session = Depends(get_session)):
    if not org:
        return _no_org()
    a = _action(session, org, action_id)
    if not a:
        return JSONResponse({"error": "Actie niet gevonden"}, status_code=404)
    if a.status == "voorgesteld":
        a.status = "geannuleerd"
        session.add(a)
        session.commit()
    return agent.action_out(a)
