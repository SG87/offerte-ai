"""Feedback: the page behind the 'Idee of bug melden' button.

For now it explains the planned flow (see docs/agent-factory-plan.md): a request goes through AI triage,
a PM agent, a human gate, developer/test/E2E agents and a daily release, and the reporter earns €20 credit
once the change is live. The form itself comes with phase 3 of that plan."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.models import Organisation
from app.web.routes import _render, _require, current_org

router = APIRouter(include_in_schema=False)

CREDIT_EUR = 20
MAX_CREDITS_PER_MONTH = 3

STEPS = [
    ("Jij", "Melden", "Je beschrijft de bug of het idee in gewone taal, met een screenshot als je wil."),
    ("AI", "Triage", "De AI stelt verdiepende vragen en kijkt of iemand dit al meldde. Dan kan je mee-stemmen."),
    ("AI", "PM-agent", "Schrijft een user story, zoekt uit wat er in de code moet gebeuren en bepaalt de prioriteit."),
    ("Mens", "Goedkeuring", "Stijn keurt de story goed, wijst ze af met een reden, of neemt ze zelf manueel op."),
    ("AI", "Developer-agent", "Maakt een aparte branch en bouwt de aanpassing."),
    ("AI", "Test-agent", "Schrijft onafhankelijk tests, met focus op veiligheid, privacy en werking."),
    ("AI", "E2E-agent", "Surft echt door de testomgeving. Breekt er iets, dan gaat het terug naar de developer-agent (max. 2 keer)."),
    ("Mens", "Review & merge", "Stijn leest de code na en keurt de samenvoeging goed."),
    ("Release", "Release", "Elke werkdag om 14 u gaat wat klaar is naar productie."),
    ("Jij", "Bericht + korting", f"Je krijgt een mail dat je vraag live staat en € {CREDIT_EUR} korting op je volgende factuur."),
]


@router.get("/feedback", response_class=HTMLResponse)
def feedback_page(request: Request, org: Organisation | None = Depends(current_org)):
    if (r := _require(request, org)) is not None:
        return r
    return _render(
        request,
        "feedback.html",
        org=org,
        steps=STEPS,
        credit=CREDIT_EUR,
        max_credits=MAX_CREDITS_PER_MONTH,
    )
