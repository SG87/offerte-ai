# Offerte-AI

AI-native offerteplatform voor vakmensen: **plan of foto in → meetstaat + offerte + geannoteerd plan uit.**
Per sector een bewerkbare *skill* (rekenregels), jouw eigen prijsconfiguratie, en drie kanalen: web-app, MCP-server (Claude) en REST-API.

Status: **v0.1 — werkende basis** (schilder + tuinaannemer, België/NL, één account per bedrijf).

## Snel starten in VS Code

Vereist: Python 3.11+ (ontwikkeld op 3.13) en VS Code met de aanbevolen extensies (Python, Ruff; VS Code stelt ze zelf voor).

1. Open de map in VS Code en voer de taak **"Setup: venv + install (eenmalig)"** uit (`Terminal → Run Task…`). Die maakt `.venv`, installeert het project en kopieert `.env.example` naar `.env`.
2. Zet eventueel je `ANTHROPIC_API_KEY` in `.env`. Zonder key draait alles, maar foto-analyse, AI-assist en skill-vertaling vallen terug op handmatige invoer en eenvoudige heuristieken.
3. Start met **F5 → "Web app (uvicorn, reload)"** of de taak "Run: web-app". Open <http://localhost:8000>.
4. Meld je aan met je **professionele e-mailadres** (bv. `info@texturepainting.be`): het platform haalt je website op, herkent de sector (schilder / tuinaannemer) en vult bedrijfsnaam, adres, btw-nummer en telefoon vooraf in. Je controleert en bevestigt.

Zonder VS Code:

```bash
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
uvicorn app.main:app --reload                            # web-app + API + MCP op :8000
pytest -q                                                # 17 tests, geen API-key nodig
python scripts/demo_quote.py tests/fixtures/sample_geometry.json   # offerte zonder web-app
```

## De flow

| Stap | Scherm / endpoint | Wat gebeurt er |
|---|---|---|
| 1. Aanmelden | `/` | E-mail → domein → website → sector + bedrijfsgegevens (Claude, met heuristische fallback). Gratis-maildomeinen worden overgeslagen. |
| 2. Onboarding | `/onboarding` | Sector bevestigen of kiezen; "andere" = sectoraanvraag + generieke skill. |
| 3. Configuratie | `/config` | Prijslijst per post (materiaal, rendement, uren/eenheid), uurtarief, marge, verlies, btw, geldigheid. Skill-aanvullingen in gewone taal → vertaald naar gestructureerde regels. Elke wijziging = nieuwe versie. |
| 4. Project | `/projects` | Klant (verplicht: kies er een of maak er meteen een aan), werfadres, type werk (bepaalt btw 6 % / 21 %), scope. |
| 5. Werkruimte | `/projects/{id}` | Project verwijderen (rechtsboven, met bevestiging; gereserveerd materiaal gaat terug naar de voorraad, mails en bestellingen blijven als historiek). Foto/plan uploaden → zones met maten (AI stelt voor, jij bevestigt schaal en aannames) → meetstaat → AI-assist ("badkamer niet meenemen, keuken 3 lagen"). |
| 6. Offerte | `/projects/{id}/offerte` | Offerte-PDF (met meetstaat als bijlage), meetstaat-Excel, geannoteerd plan (SVG). **Per e-mail naar de klant** (afzender *Offer AI*, PDF in bijlage, tekst in de taal van de klant en aanpasbaar, net vóór de groet de offertegegevens en de knoppen *Offerte aanvaarden* / *weigeren*); eenmaal verzonden gaat het project naar *Offerte gestuurd*. De knoppen openen de klantpagina `/o/{token}` (geen login, één geheime link per offerteversie): de klant bekijkt de offerte, downloadt de PDF en bevestigt met naam + akkoord-vinkje. Aanvaard → project *Offerte aanvaard* (voorraad en planning starten), geweigerd → *Offerte geweigerd*; je krijgt een mail. Een nieuwere versie maakt de oude link ongeldig. Zet `OFFERTE_BASE_URL` op het publieke adres, anders werken de links niet bij de klant. Versies worden bijgehouden. B2C-klant: prijzen incl. btw; B2B-klant: prijzen excl. btw, met btw en totaal incl. onderaan. |

## AI in de achtergrond

Alle AI-aanroepen in de web-app lopen als achtergrondtaak (`app/ai_jobs.py`): bedrijfsopzoeking bij het aanmaken van een account of via Account → "Opnieuw opzoeken", plan lezen, AI-assist en het vertalen van skill-aanvullingen. Je krijgt een pop-up dat de AI gestart is en kan verder werken op andere pagina's; rechtsboven staat **✦ AI bezig** (klik voor de lijst met timers) en een melding met link zodra het klaar of mislukt is. De pagina waar het resultaat hoort (project, onboarding, configuratie, account) ververst zichzelf. REST-API en MCP blijven synchroon antwoorden.

## Account

`/account` (tandwiel rechtsboven): bedrijfsgegevens en login-e-mail, taal en land, contactpersoon aankoop, huisstijl (logo, kleuren), sector (nieuwe prijslijstversie met de standaardprijzen van die sector), gegevens opnieuw ophalen met AI (enkel lege velden, of alles overschrijven), API-sleutel tonen/kopiëren/vernieuwen, en de status van de integraties uit `.env`.

## Projectbeheer

Elk project doorloopt deze statussen (`app/workflow.py`):

Offerte voorbereid → Offerte gemaakt → Offerte gestuurd → Offerte aanvaard → Werk in uitvoering → Werk afgerond → Factuur verstuurd → Factuur betaald → Afgerond. **Offerte geweigerd** sluit het project af (kan tot en met "gestuurd").

- Een nieuw project start in *voorbereid*; de eerste berekende offerte zet het op *gemaakt*. De rest zet je zelf (projectpagina, `POST /api/v1/projects/{id}/status` of MCP-tool `set_project_status`). Elke stap kan één stap terug (vergissing); alles komt in de historiek.
- **Bij "Offerte aanvaard"** lopen twee triggers, elk hooguit één keer per project:
  1. **Voorraadcontrole** — het materiaal uit de meetstaat (posten met *voorraad bijhouden*, bv. verf, tegels, planten) tegenover de voorraad op de configuratiepagina. Bij een tekort gaat een aankooplijst naar de *contactpersoon aankoop*. Event `voorraad.controle` voor de toekomstige voorraad-agent.
  2. **Inplannen** — event `werk.inplannen` (klant, werfadres, uren, scope) voor de planningstool.
- **Voorstel uitvoering**: niet bij het aanmaken, maar zodra het plan gelezen is (AI-analyse in de web-app/API, of Claude via `quote_from_zones`) stelt het platform op basis van de uren uit de zones een ploeg en een periode voor (bij manueel ingevoerde zones met de knop "Stel uitvoering voor"; een herberekende offerte verfijnt een bestaand voorstel): de eerste vrije werkdagen vanaf een week later waarop genoeg werknemers met een rol op de werf (vakman, ploegbaas) vrij zijn — geen verlof, niets anders ingepland — en daarvan de minst belaste. Richtlijn: 1 persoon per 40 u (max. 3), 8 u per persoon per dag. Je past het aan op de projectpagina; de zin "Voorgestelde uitvoering: … onder voorbehoud van uw akkoord en onze beschikbaarheid" staat op de offerte (pagina en PDF). Bij "Offerte aanvaard" wordt het voorstel het planningsblok.
- **Facturen** (`/facturen`): bij "Werk afgerond" ontstaat een conceptfactuur uit de laatste offerte (in de taal van de klant) en een to-do "Factuur goedkeuren" voor de werknemer met *Facturatie*. In concept pas je ze aan (manueel of met een AI-opdracht; bedragen rekent de app). Goedkeuren geeft een doorlopend nummer zonder gaten, factuurdatum en gestructureerde mededeling (OGM). B2B: "Verstuur via Peppol" (UBL 2.1 / Peppol BIS Billing 3.0 naar `OFFERTE_PEPPOL_API_URL`; zonder koppeling: UBL downloaden). Particulier: PDF tonen en mailen. Verstuurd/betaald zetten de projectstatus mee. IBAN/BIC staan op Account. API: `/api/v1/invoices`.
- **AI-assistent** (knop "✦ Vraag het AI", paneel rechts): beantwoordt vragen over projecten, planning, voorraad, facturen, klanten en taken met je eigen gegevens. Wijzigingen (status, factuur aanpassen/goedkeuren, taak afvinken) stelt hij voor als kaart; ze gebeuren pas na "Uitvoeren".
- **Voorraad** (`/voorraad`): per materiaal *in voorraad*, *gereserveerd* en *beschikbaar*, bestelpunt, verpakking, leverancier. Bij "Offerte aanvaard" wordt het materiaal van de offerte **gereserveerd**; wat tekort is (of onder het bestelpunt zakt) wordt een **bestelvoorstel** per leverancier. De verantwoordelijke voorraad krijgt een to-do en een mail, bevestigt in het platform, en dan gaat de **bestelmail naar de leverancier**. Die mail heeft een knop *Bevestig ontvangst & levering*: de leverancier opent `/l/{token}` (geen login), bevestigt de bestelling en geeft per lijn *leverbaar / nalevering / niet leverbaar*, de verwachte leverdatum en een opmerking (aan te passen tot de levering binnen is). De verantwoordelijke voorraad krijgt een mail, de to-do *levering controleren* schuift naar de laatste leverdatum en wat niet leverbaar is wordt een to-do *alternatief zoeken*; de voorraadpagina toont het antwoord per lijn. "Ontvangen" boekt de levering in; "Werk afgerond" boekt het gereserveerde materiaal af.
- **To-do** (`/todo`): wat er moet gebeuren — bestelling bevestigen, levering inboeken, werk inplannen, factuur versturen, betaling opvolgen — plus eigen to-do's. Het platform zet ze er zelf op en vinkt ze af als de stap gezet is.
- **Planning** (`/planning`): maandkalender met werkblokken per project en de werknemers die het doen. Afwezige werknemers (verlof, ziekte, opleiding — in te geven op hun pagina onder Contacten) kunnen niet ingepland worden.
- **Contacten** (`/contacten`): klanten (B2C of B2B, en een taal NL/FR/EN: de offerte en PDF komen in die taal — vaste teksten uit `app/i18n.py`, postnamen/ruimtes/aannames vertaald door Claude, één keer per offerteversie; elk project hoort bij een klant), leveranciers (bestellingen gaan naar hun e-mail) en werknemers (rol, kleur in de planning, *verantwoordelijke voorraad* en *planner*).
- Integraties zijn nog placeholders met een echte aansluiting: mails gaan via SMTP als `OFFERTE_SMTP_HOST` gezet is (anders blijven ze in de outbox), events worden naar `OFFERTE_WEBHOOK_URL` gePOST (HMAC-ondertekend) of opgehaald met `GET /api/v1/events` en afgevinkt met `POST /api/v1/events/{id}/ack`. Een agent zet de voorraad met `PUT /api/v1/stock`, leest bestellingen met `GET /api/v1/orders` en bevestigt/ontvangt ze met `POST /api/v1/orders/{id}/bevestig|ontvangen|annuleer`; to-do's via `/api/v1/todos`, planning via `GET /api/v1/planning?van=&tot=`.

## Architectuur: LLM interpreteert, de engine rekent

Het belangrijkste ontwerpprincipe: **de AI rekent nooit zelf.** Claude zet een foto of instructie om naar gestructureerde data (`Geometry`, `Adjustment`); alle rekenwerk gebeurt deterministisch in `app/engine` en is per regel herleidbaar (bruto, aftrek, netto, uren).

```
foto / plan / maten ──► app/ai/plan_analysis ──► Geometry (zones, openingen, aannames)
                                                     │  gebruiker bevestigt schaal + aannames
skills/<sector>/rules.yaml ──► app/engine/skills ────┤
jouw aanvullingen (tekst) ──► app/ai/skill_assist ──► Adjustment[] ──┐
jouw prijzen ──► PriceConfig ────────────────────────────────────────┤
                                                                     ▼
                                              app/engine/calculator.calculate_quote()
                                                                     ▼
                                   QuoteResult: meetstaat-regels, totalen, btw, aannames, indicatief?
                                                                     ▼
                                 app/outputs: offerte.pdf · meetstaat.xlsx · plan.svg
```

Een offerte is **indicatief** zolang de schaal niet bevestigd is of er onbevestigde aannames zijn (bv. plafondhoogte uit de configuratie). Dat staat op de offerte zelf.

### Mappen

```
app/
  main.py          FastAPI: web-app + /api/v1 + /mcp in één proces
  config.py        instellingen (.env)
  models.py        tabellen (SQLite via SQLModel): Organisation, ConfigVersion, SkillOverride, Project, Plan, Quote, SectorRequest
  schemas.py       contracten: Geometry, Zone, Opening, Assumption, PriceConfig, Adjustment, QuoteResult, CompanyLookup
  services.py      use-cases, gedeeld door web, API en MCP
  engine/          deterministische rekenkern (geen LLM-calls)
  ai/              Claude: plan-analyse, skill-vertaling, AI-assist, bedrijfslookup (alles met fallback zonder key)
  outputs/         PDF (reportlab), Excel (openpyxl), SVG
  web/             Jinja-templates + CSS
  api/routes.py    REST API v1 (X-API-Key)
  mcp_server.py    MCP-server (stdio of HTTP)
skills/<sector>/   rules.yaml (posten, parameters, voorbeeldprijzen) + SKILL.md (instructies voor de AI)
scripts/           demo zonder web-app
tests/             pytest (engine, lookup, API + web-flow, MCP-mount)
```

## Skills: de rekenregels per sector

Een skill is een map `skills/<sector>/` met twee bestanden die je zonder Python kan aanpassen:

- `rules.yaml` — parameters (standaard lagen, dagkantdiepte, drempel hoge plafonds…), **posten** (code, label, maatregel zoals `wall_net` of `area`, scope, lagen, voorwaarden) en voorbeeldprijzen die bij onboarding naar de gebruiker gekopieerd worden.
- `SKILL.md` — wat de AI uit een plan moet halen voor deze sector en welke aannames ze moet melden.

Beschikbare maatregelen: `wall_net` (omtrek × hoogte − openingen ≥ 1 m² + dagkanten), `ceiling`, `floor`, `perimeter_minus_doors`, `doors_count`, `windows_count`, `area`, `length`, `count`, `volume`, `per_zone`. Onbekende namen geven een fout, nooit een stille misrekening.

Gebruikers overschrijven de basis via **aanvullingen in gewone taal** (Configuratie). Die worden vertaald naar `Adjustment`-objecten: `set_param`, `tag_zones`, `exclude_zones`, `set_layers`, `disable_post`, `hours_factor`. Wat niet in die operaties past, wordt gemeld als "niet vertaald".

Nieuwe sector toevoegen = nieuwe map met `rules.yaml` + `SKILL.md`; de app pikt ze automatisch op.

## REST-API

Interactieve docs op <http://localhost:8000/docs>. Authenticatie via `X-API-Key` (te vinden op de configuratiepagina of via `/api/v1/signup`).

```bash
# account + key
curl -s localhost:8000/api/v1/signup -H 'content-type: application/json' -d '{"email":"info@jouwbedrijf.be"}'

# klant (b2c = offerte incl. btw, b2b = excl. btw); een project zonder klant wordt geweigerd
curl -s localhost:8000/api/v1/customers -H "X-API-Key: $KEY" -H 'content-type: application/json' \
     -d '{"name":"Bouwbedrijf Vos bv","kind":"b2b","vat_number":"BE0123456789"}'
# project → plan → offerte (client_name: bestaande klant met die naam, anders een nieuwe b2c-klant)
curl -s localhost:8000/api/v1/projects -H "X-API-Key: $KEY" -H 'content-type: application/json' \
     -d '{"name":"Binnenschilderwerk gelijkvloers","client_name":"Fam. Janssens","scope":["muren","plafonds"]}'
curl -s localhost:8000/api/v1/projects/1/plan -H "X-API-Key: $KEY" -F file=@plan.jpg
curl -s -X POST localhost:8000/api/v1/projects/1/quote -H "X-API-Key: $KEY"
curl -s localhost:8000/api/v1/projects/1/quote.pdf -H "X-API-Key: $KEY" -o offerte.pdf
# offerte mailen naar de klant (leeg = adres van de klant en standaardtekst)
curl -s -X POST localhost:8000/api/v1/projects/1/quote/mail -H "X-API-Key: $KEY" -H 'content-type: application/json' -d '{"to":"klant@voorbeeld.be"}'
curl -s -X DELETE localhost:8000/api/v1/projects/1 -H "X-API-Key: $KEY"   # definitief

# stateless rekenen met jouw configuratie (handig voor integraties)
curl -s localhost:8000/api/v1/quote/preview -H "X-API-Key: $KEY" -H 'content-type: application/json' \
     -d @tests/fixtures/sample_geometry.json   # + "scope"
```

## MCP: "neem een foto van het plan in Claude en krijg een offerte"

De Claude-app ziet de foto al. Daarom leest Claude het plan zelf (met de sector-instructies uit de prompt `plan_lezen`), geeft de zones met maten door aan `quote_from_zones`, en rekent de server deterministisch. Tools: `get_pricing_config`, `list_customers`, `quote_from_zones` (met `customer_id`, of `client_name` + `client_type` b2c/b2b), `analyze_plan_image_url`, `update_skill_override`, `request_sector`.

**Claude Desktop / Claude Code (stdio)** — `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "offerte-ai": {
      "command": "/volledig/pad/offerte-ai/.venv/bin/python",
      "args": ["-m", "app.mcp_server"],
      "env": {
        "PYTHONPATH": "/volledig/pad/offerte-ai",
        "OFFERTE_DATABASE_URL": "sqlite:////volledig/pad/offerte-ai/data/offerte.db",
        "OFFERTE_DATA_DIR": "/volledig/pad/offerte-ai/data",
        "OFFERTE_BASE_URL": "http://localhost:8000",
        "OFFERTE_MCP_ORG_EMAIL": "info@jouwbedrijf.be"
      }
    }
  }
}
```

**Claude-app op je gsm (remote MCP)** — de web-app serveert dezelfde server op `http://localhost:8000/mcp`. Zet die achter een tunnel (bv. `cloudflared tunnel --url http://localhost:8000` of ngrok), voeg de tunnel-hostnaam toe aan `OFFERTE_MCP_ALLOWED_HOSTS` in `.env` (DNS-rebinding-bescherming; anders antwoordt `/mcp` met 421) en voeg de URL `…/mcp` toe als custom connector in claude.ai. Let op: in v0.1 heeft `/mcp` nog geen authenticatie; zet dit alleen tijdelijk open en alleen voor jezelf. Authenticatie (OAuth of API-key) staat op de roadmap.

Maak eerst een account aan in de web-app: de MCP-server werkt voor de organisatie uit `OFFERTE_MCP_ORG_EMAIL` (anders de eerste organisatie in de database).

## Beslissingen (24-10-2026)

| Onderwerp | Keuze |
|---|---|
| Sectoren v1 | Schilder, tuinaannemer (+ generieke skill en sectoraanvraag) |
| Invoer v1 | Foto van een plan (PDF/scan werkt ook; vector-PDF-parsing later) |
| Meetfouten | Platform draagt geen risico: gebruiker bevestigt schaal en aannames, offerte is "indicatief" tot dan, disclaimer in app en op de offerte |
| Prijsmodel | Later |
| Markt / taal | België, Nederlands eerst |
| MCP | In v1, voor de vakman zelf (Claude-app) |
| Integraties (boekhouding, online akkoord) | Placeholders in v1; e-mail werkt via SMTP |
| Skill-aanpassing | Vrije tekst, door AI vertaald |
| Accounts | Eén account per bedrijf (login op e-mail; Google/OAuth later) |

## Roadmap (voorstel)

1. Authenticatie op `/mcp` en magic-link/Google-login voor de web-app.
2. Vector-PDF-parsing (echte polygonen → exact geannoteerd plan; nu schematisch bij foto's).
3. Interactief plan: zones aanklikken en post wijzigen in de browser (nu via tabel + AI-assist).
4. Online akkoord, koppeling boekhouding (Teamleader/Billit/… — nog te kiezen).
5. Leveranciersprijslijsten importeren; KBO-lookup voor bedrijfsgegevens.
6. Meer sectoren (stukadoor, vloerder, dakwerker) op basis van sectoraanvragen.

## Omgevingsvariabelen

| Variabele | Doel |
|---|---|
| `ANTHROPIC_API_KEY` | AI-functies aan (zonder: handmatig + heuristieken) |
| `ANTHROPIC_MODEL` | standaard `claude-sonnet-5-5` |
| `OFFERTE_SECRET_KEY` | sessiecookie (wijzig in productie) |
| `OFFERTE_DATABASE_URL` | standaard `sqlite:///./data/offerte.db` |
| `OFFERTE_DATA_DIR` | uploads en outputs (standaard `./data`) |
| `OFFERTE_BASE_URL` | basis voor links die de MCP-server teruggeeft |
| `OFFERTE_MCP_ORG_EMAIL` | organisatie waarvoor de MCP-server werkt |
| `OFFERTE_SMTP_HOST` … `OFFERTE_SMTP_FROM` | mailserver voor offertes, bestelvoorstellen en bestellingen (leeg = outbox). Poort 465 = SSL, anders STARTTLS. Testen: Account → Integraties → *Stuur testmail* |
| `OFFERTE_MAIL_FROM_NAME` | afzendernaam van de mails (standaard `Offer AI`) |
| `OFFERTE_MAIL_REDIRECT_TO` | testmodus: alle mails naar dit adres i.p.v. naar klanten en leveranciers |
| `OFFERTE_WEBHOOK_URL`, `OFFERTE_WEBHOOK_SECRET` | triggers voor voorraad-agent en planningstool |
| `OFFERTE_MCP_ALLOWED_HOSTS` | Host-headers die `/mcp` aanvaardt (standaard localhost/127.0.0.1; voeg je tunnel-host toe) |

## Disclaimer

Offerte-AI rekent op basis van de door jou aangeleverde plannen, maten en prijzen. Jij controleert de meetstaat en de offerte vóór verzending; het platform draagt geen aansprakelijkheid voor meet- of rekenfouten in verzonden offertes.
