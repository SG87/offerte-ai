"""AI agent pane: read tools answer from the org's data, actions only run after the user's click, and the
conversation is stored append-only (thinking blocks kept as received)."""

import copy
import json
from pathlib import Path

from sqlmodel import select

from app.ai import agent
from app.config import get_settings
from app.db import session_scope
from app.models import Invoice

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "sample_geometry.json").read_text())


def _login(client, email):
    client.cookies.clear()
    client.post("/signup", data={"email": email})
    client.post("/onboarding", data={"sector": "schilder"})


def test_agent_answers_with_tools_and_runs_actions_only_after_confirmation(client, monkeypatch):
    _login(client, "agent@gmail.com")
    h = {"X-API-Key": client.post("/api/v1/signup", json={"email": "agent@gmail.com", "lookup": False}).json()["api_key"]}
    pid = client.post("/api/v1/projects", headers=h, json={"name": "Gevel", "client_name": "Fam. Claes"}).json()["project_id"]
    client.put(f"/api/v1/projects/{pid}/geometry", headers=h, json=FIXTURE)
    client.post(f"/api/v1/projects/{pid}/quote", headers=h)
    for to in ["gestuurd", "aanvaard", "in_uitvoering", "werk_afgerond"]:
        client.post(f"/api/v1/projects/{pid}/status", headers=h, json={"to": to})
    with session_scope() as s:
        iid = s.exec(select(Invoice).where(Invoice.project_id == pid)).one().id

    page = client.get("/projects").text
    assert 'id="agent-toggle"' in page and 'id="agent-pane"' in page

    # without AI the pane says so
    r = client.post("/agent/vraag", json={"message": "hallo"})
    assert r.status_code == 503 and "AI staat uit" in r.json()["error"]

    calls = []
    script = [
        ([{"type": "thinking", "thinking": "", "signature": "sig-1"},
          {"type": "tool_use", "id": "t1", "name": "facturen", "input": {"status": "concept"}}], "tool_use"),
        ([{"type": "tool_use", "id": "t2", "name": "factuur_goedkeuren", "input": {"invoice_id": iid}}], "tool_use"),
        ([{"type": "text", "text": f"Er is 1 conceptfactuur. Ik stel voor om [ze](/facturen/{iid}) goed te keuren."}], "end_turn"),
    ]

    def fake_call(system, messages, tools):
        calls.append(copy.deepcopy(messages))
        assert "Fam. Claes" not in system and "agent@gmail.com" in system  # company name falls back to the login
        assert {t["name"] for t in tools} >= {"facturen", "factuur_goedkeuren", "project_details"}
        return script.pop(0)

    monkeypatch.setattr(get_settings(), "anthropic_api_key", "test-key")
    monkeypatch.setattr(agent, "_call", fake_call)
    out = client.post("/agent/vraag", json={"message": "Welke facturen moet ik nog goedkeuren?"}).json()
    conv = out["conversation_id"]
    assert f"[ze](/facturen/{iid})" in out["reply"]
    (action,) = out["actions"]
    assert action["status"] == "voorgesteld" and "goedkeuren" in action["summary"]
    # the read tool got the real data; the action did not run
    tool_result = json.loads(calls[1][-1]["content"][0]["content"])
    assert tool_result[0]["invoice_id"] == iid and tool_result[0]["status"] == "concept"
    assert calls[1][-2]["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig-1"}  # sent back unchanged
    with session_scope() as s:
        assert s.get(Invoice, iid).status == "concept"

    # the user clicks 'Uitvoeren'
    done = client.post(f"/agent/acties/{action['id']}/uitvoeren").json()
    assert done["status"] == "uitgevoerd" and "goedgekeurd" in done["result"]
    with session_scope() as s:
        assert s.get(Invoice, iid).status == "goedgekeurd"
    assert client.post(f"/agent/acties/{action['id']}/uitvoeren").json()["status"] == "uitgevoerd"  # no second run

    # next question: history is only appended to, and the agent hears what happened to its proposal
    script.append(([{"type": "text", "text": "Top, de factuur is goedgekeurd."}], "end_turn"))
    client.post("/agent/vraag", json={"message": "En nu?", "conversation_id": conv})
    last = calls[-1]
    assert last[: len(calls[2])] == calls[2]  # earlier turns untouched (append-only)
    assert last[-1]["role"] == "user" and last[-1]["content"].startswith("[Melding van het platform]")
    assert "uitgevoerd" in last[-1]["content"] and last[-1]["content"].endswith("En nu?")

    hist = client.get(f"/agent/gesprek/{conv}").json()
    assert [m["role"] for m in hist["messages"]] == ["user", "assistant", "user", "assistant"]
    assert hist["messages"][2]["text"] == "En nu?" and hist["actions"][0]["status"] == "uitgevoerd"

    # another company cannot see or run it
    _login(client, "anderagent@gmail.com")
    assert client.get(f"/agent/gesprek/{conv}").status_code == 404
    assert client.post(f"/agent/acties/{action['id']}/annuleren").status_code == 404


def test_bad_tool_input_goes_back_to_the_model_as_an_error(client, monkeypatch):
    _login(client, "agentfout@gmail.com")
    script = [
        ([{"type": "tool_use", "id": "x1", "name": "project_status_wijzigen", "input": {"project_id": 999999, "status": "aanvaard"}}], "tool_use"),
        ([{"type": "text", "text": "Dat project bestaat niet."}], "end_turn"),
    ]
    seen = []

    def fake_call(system, messages, tools):
        seen.append(copy.deepcopy(messages))
        return script.pop(0)

    monkeypatch.setattr(get_settings(), "anthropic_api_key", "test-key")
    monkeypatch.setattr(agent, "_call", fake_call)
    out = client.post("/agent/vraag", json={"message": "Zet project 999999 op aanvaard"}).json()
    assert out["actions"] == [] and out["reply"] == "Dat project bestaat niet."
    result = seen[1][-1]["content"][0]
    assert result["is_error"] is True and "niet gevonden" in result["content"]
