"""The general AI agent in the side pane: answers questions about the company's data with read tools and
proposes changes as actions the user confirms.

Conversations are stored exactly as exchanged with the API (thinking blocks included) and only appended to,
so earlier turns are never edited. The system prompt is fixed per conversation for the same reason."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

from sqlmodel import Session, select

from app.ai import agent_tools
from app.ai.client import get_client
from app.config import get_settings
from app.models import AgentAction, AgentConversation, Organisation

MAX_STEPS = 10
# models that accept the server-side refusal fallback ("default" picks the fallback model per refusal category)
FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"}

SYSTEM = """Je bent de AI-assistent in Offerte-AI, het platform waarmee {company} ({sector}) offertes, projecten,
planning, voorraad en facturen beheert. Vandaag is het {today}.

- Beantwoord vragen met de tools: zoek gegevens op in plaats van te gokken, en noem nooit bedragen of datums
  die je niet uit een tool hebt.
- Antwoord kort en concreet, in de taal van de gebruiker (meestal Nederlands). Bedragen in euro met komma
  (€ 1.234,56). Verwijs naar pagina's met een link in de vorm [naam](/pad) — enkel paden uit de tools.
- Wijzigingen voer je nooit zelf uit. Gebruik daarvoor een actietool: die maakt een voorstel met een knop
  'Uitvoeren' voor de gebruiker. Zeg dan duidelijk dat het een voorstel is dat nog bevestigd moet worden.
- Krijg je een melding dat een voorgestelde actie uitgevoerd of geannuleerd werd, ga daar dan van uit.
- Is iets niet mogelijk in het platform, zeg dat eerlijk."""


def new_conversation(session: Session, org: Organisation) -> AgentConversation:
    conv = AgentConversation(
        org_id=org.id,
        system=SYSTEM.format(company=org.company_name or org.email, sector=org.sector, today=date.today().isoformat()),
    )
    session.add(conv)
    session.commit()
    session.refresh(conv)
    return conv


def get_conversation(session: Session, org: Organisation, conversation_id: int | None) -> AgentConversation | None:
    conv = session.get(AgentConversation, conversation_id) if conversation_id else None
    return conv if conv and conv.org_id == org.id else None


def _call(system: str, messages: list[dict], tools: list[dict]) -> tuple[list[dict], str]:
    """One API round: (content blocks as plain dicts, stop reason). Kept separate so tests can replace it."""
    settings = get_settings()
    client = get_client()
    kwargs: dict[str, Any] = dict(
        model=settings.anthropic_model,
        max_tokens=16000,
        system=system,
        messages=messages,
        tools=tools,
        output_config={"effort": "medium"},
    )
    if settings.anthropic_model in FALLBACK_MODELS:
        msg = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    else:
        msg = client.messages.create(**kwargs)
    return [b.model_dump(mode="json", exclude_none=True) for b in msg.content], msg.stop_reason


def _text(content: list[dict] | str) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip()


def action_out(a: AgentAction) -> dict:
    return {"id": a.id, "name": a.name, "summary": a.summary, "status": a.status, "result": a.result}


def _outcomes_since_last_turn(session: Session, conv: AgentConversation) -> str:
    """Tell the agent which of its proposals the user ran or cancelled since the previous question."""
    done = session.exec(
        select(AgentAction).where(AgentAction.conversation_id == conv.id, AgentAction.status != "voorgesteld",
                                  AgentAction.reported == False)  # noqa: E712
    ).all()
    lines = []
    for a in done:
        lines.append(f"- actie #{a.id} ({a.summary}): {a.status}" + (f" — {a.result}" if a.result else ""))
        a.reported = True
        session.add(a)
    return ("[Melding van het platform]\n" + "\n".join(lines) + "\n\n") if lines else ""


def ask(session: Session, org: Organisation, conv: AgentConversation, question: str) -> dict:
    """Run one user turn (tool calls included). Returns {reply, actions} — actions proposed in this turn."""
    messages = conv.messages
    messages.append({"role": "user", "content": _outcomes_since_last_turn(session, conv) + question})
    tools = agent_tools.tool_definitions()
    proposed: list[AgentAction] = []
    reply = ""
    for _ in range(MAX_STEPS):
        content, stop = _call(conv.system, messages, tools)
        messages.append({"role": "assistant", "content": content})
        if stop == "refusal":
            reply = "Daar kan ik je niet mee helpen."
            break
        uses = [b for b in content if b.get("type") == "tool_use"]
        if stop != "tool_use" or not uses:
            reply = _text(content) or ("Het antwoord werd te lang en is afgebroken." if stop == "max_tokens" else "")
            break
        results = []
        for use in uses:
            out, is_error = _run_tool(session, org, conv, use["name"], use.get("input") or {}, proposed)
            block = {"type": "tool_result", "tool_use_id": use["id"], "content": json.dumps(out, ensure_ascii=False, default=str)}
            if is_error:
                block["is_error"] = True
            results.append(block)
        messages.append({"role": "user", "content": results})
    else:
        reply = "Ik heb te veel stappen nodig voor deze vraag; probeer ze op te splitsen."
    conv.messages_json = json.dumps(messages, ensure_ascii=False)
    conv.title = conv.title or question[:80]
    conv.updated_at = datetime.now(UTC)
    session.add(conv)
    session.commit()
    return {"reply": reply, "actions": [action_out(a) for a in proposed]}


def _run_tool(session: Session, org: Organisation, conv: AgentConversation, name: str, args: dict,
              proposed: list[AgentAction]) -> tuple[Any, bool]:
    try:
        if name in agent_tools.READ:
            fn, _ = agent_tools.READ[name]
            return fn(session, org, **args), False
        if name in agent_tools.ACTIONS:
            describe, _, _, _ = agent_tools.ACTIONS[name]
            action = AgentAction(org_id=org.id, conversation_id=conv.id, name=name,
                                 input_json=json.dumps(args, ensure_ascii=False), summary=describe(session, org, args))
            session.add(action)
            session.commit()
            session.refresh(action)
            proposed.append(action)
            return {"voorgesteld": True, "actie_id": action.id, "samenvatting": action.summary,
                    "let_op": "Nog NIET uitgevoerd: de gebruiker ziet een knop 'Uitvoeren'."}, False
        return {"error": f"Onbekende tool {name}"}, True
    except Exception as exc:  # bad arguments or not allowed: the model gets the reason and can correct itself
        session.rollback()
        return {"error": f"{type(exc).__name__}: {exc}"}, True


def run_action(session: Session, org: Organisation, action: AgentAction) -> AgentAction:
    """The user clicked 'Uitvoeren'."""
    if action.status != "voorgesteld":
        return action
    _, run, _, _ = agent_tools.ACTIONS[action.name]
    try:
        action.result, action.status = run(session, org, action.input), "uitgevoerd"
    except Exception as exc:
        session.rollback()
        action.result, action.status = f"{exc}", "mislukt"
    session.add(action)
    session.commit()
    return action


def history(session: Session, conv: AgentConversation) -> dict:
    """What the pane shows: the user's questions and the agent's answers (tool traffic left out), and actions."""
    shown = []
    for m in conv.messages:
        if m["role"] == "user" and isinstance(m["content"], str):
            text = m["content"].split("\n\n", 1)[1] if m["content"].startswith("[Melding van het platform]") else m["content"]
            shown.append({"role": "user", "text": text})
        elif m["role"] == "assistant" and (text := _text(m["content"])):
            shown.append({"role": "assistant", "text": text})
    actions = session.exec(select(AgentAction).where(AgentAction.conversation_id == conv.id).order_by(AgentAction.id)).all()
    return {"conversation_id": conv.id, "messages": shown, "actions": [action_out(a) for a in actions]}
