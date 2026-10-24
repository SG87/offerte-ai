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
| 4. Project | `/projects` | Klant, werfadres, type werk (bepaalt btw 6 % / 21 %), scope. |
| 5. Werkruimte | `/projects/{id}` | Foto/plan uploaden → zones met maten (AI stelt voor, jij bevestigt schaal en aannames) → meetstaat → AI-assist ("badkamer niet meenemen, keuken 3 lagen"). |
| 6. Offerte | `/projects/{id}/offerte` | Offerte-PDF (met meetstaat als bijlage), meetstaat-Excel, geannoteerd plan (SVG). Versies worden bijgehouden. |

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

# project → plan → offerte
curl -s localhost:8000/api/v1/projects -H "X-API-Key: $KEY" -H 'content-type: application/json' \
     -d '{"name":"Binnenschilderwerk gelijkvloers","client_name":"Fam. Janssens","scope":["muren","plafonds"]}'
curl -s localhost:8000/api/v1/projects/1/plan -H "X-API-Key: $KEY" -F file=@plan.jpg
curl -s -X POST localhost:8000/api/v1/projects/1/quote -H "X-API-Key: $KEY"
curl -s localhost:8000/api/v1/projects/1/quote.pdf -H "X-API-Key: $KEY" -o offerte.pdf

# stateless rekenen met jouw configuratie (handig voor integraties)
curl -s localhost:8000/api/v1/quote/preview -H "X-API-Key: $KEY" -H 'content-type: application/json' \
     -d @tests/fixtures/sample_geometry.json   # + "scope"
```

## MCP: "neem een foto van het plan in Claude en krijg een offerte"

De Claude-app ziet de foto al. Daarom leest Claude het plan zelf (met de sector-instructies uit de prompt `plan_lezen`), geeft de zones met maten door aan `quote_from_zones`, en rekent de server deterministisch. Tools: `get_pricing_config`, `quote_from_zones`, `analyze_plan_image_url`, `update_skill_override`, `request_sector`.

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
| Integraties (boekhouding, e-mail, online akkoord) | Placeholders in v1 |
| Skill-aanpassing | Vrije tekst, door AI vertaald |
| Accounts | Eén account per bedrijf (login op e-mail; Google/OAuth later) |

## Roadmap (voorstel)

1. Authenticatie op `/mcp` en magic-link/Google-login voor de web-app.
2. Vector-PDF-parsing (echte polygonen → exact geannoteerd plan; nu schematisch bij foto's).
3. Interactief plan: zones aanklikken en post wijzigen in de browser (nu via tabel + AI-assist).
4. E-mail verzenden, online akkoord, koppeling boekhouding (Teamleader/Billit/… — nog te kiezen).
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
| `OFFERTE_MCP_ALLOWED_HOSTS` | Host-headers die `/mcp` aanvaardt (standaard localhost/127.0.0.1; voeg je tunnel-host toe) |

## Disclaimer

Offerte-AI rekent op basis van de door jou aangeleverde plannen, maten en prijzen. Jij controleert de meetstaat en de offerte vóór verzending; het platform draagt geen aansprakelijkheid voor meet- of rekenfouten in verzonden offertes.
