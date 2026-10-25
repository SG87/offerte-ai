# Agent factory: plan and decisions

Status: approved plan, 2026-10-25. Owner and sole human gate: Stijn Geuens (stijn@recommendit.net, GitHub SG87).

Goal: customers report bugs and request features in Offerte-AI. A pipeline of AI agents turns an approved
request into a tested, reviewed, deployed change. The reporter gets €20 credit on the next invoice once the
change is released. The pipeline itself lives in a separate, reusable repository ("the factory") so other
products can use it later.

## 1. Decisions

| Topic | Decision | Why |
|---|---|---|
| Hosting | Azure **App Service for Containers** (Linux), one app for staging, one for production, each with its own database. Not a Function App. | FastAPI is a long-running process with in-process background jobs and an HTTP MCP endpoint. Functions are request-scoped and would force a rewrite of `app/ai_jobs.py`. |
| Database | **Azure Database for PostgreSQL Flexible Server** (Burstable B1ms to start). SQLite stays for local dev and tests. Migrations with **Alembic**; the ad-hoc `ALTER TABLE` logic in `app/db.py` is retired. | Managed backups, point-in-time restore, concurrent writers, safe schema changes shipped by agents. SQLModel already speaks Postgres. |
| Files | **Azure Blob Storage** for logos, uploaded plans and generated PDFs; `OFFERTE_DATA_DIR` becomes a storage backend abstraction with a local-disk implementation for dev. | The container filesystem is not durable across deploys. |
| Container registry | **Azure Container Registry**, images tagged with the git SHA and the release tag. | Rollback = redeploy previous tag. |
| CI/CD | **GitHub Actions**, OIDC login to Azure (no stored cloud secrets). | Same place the agents run. |
| Billing | **Stripe Billing** (subscription per Organisation, Stripe Customer Portal for cards). The €20 credit is a **Stripe customer balance** entry, which Stripe applies to the next invoice automatically. | No home-made invoicing or credit arithmetic; the ledger in our DB mirrors Stripe for display and audit. |
| Admin | `OFFERTE_ADMIN_EMAILS` setting (comma separated). An Organisation whose login email is listed sees the admin pages. Only stijn@recommendit.net for now. No multi-user accounts yet. | Smallest change that gives a real gate. |
| Human gate location | In the app **and** on the GitHub issue, kept in sync via labels. The app is the primary UI; a label change on GitHub also counts. | Approving from the phone via GitHub is convenient; the app page shows customer context GitHub does not have. |
| Backlog | **GitHub Issues** are the source of truth. The public backlog page in the app is a read-only view. Upvotes are 👍 reactions plus a local follower row. | Dynamic backlog, history, API and Projects board for free. |
| Claude in CI | Anthropic **API key** stored as a GitHub secret, with a monthly budget cap in the Anthropic console and a **per-issue cost cap of €15**. Hitting the cap aborts the run and labels the issue `needs-human`. | Predictable spend; no OAuth token refresh problems in CI. |
| E2E | **Playwright** headless in GitHub Actions against staging, seeded test organisation, `OFFERTE_MAIL_REDIRECT_TO` set so no real mail leaves. | Deterministic, no dependency on a desktop browser. |
| Feedback loop | E2E or test failures are posted as a structured comment; the developer agent re-runs with that comment. **Maximum 2 loops**, then `needs-human`. | Bounded cost and no infinite ping-pong. |
| Release | GitHub Actions cron on **working days at 14:00 Europe/Brussels** (two UTC crons, the job checks the local hour and the weekday). Releases whatever is on `main` if it moved since the last tag. Tag `vYYYY.MM.DD`, build image, run migrations, deploy production, send `released` webhook. | Timing is not critical; working days keep you available if something breaks. |
| Credits | Credit only on **release**, never on approval. One credit per issue to the **first** reporter. **Max 3 credits per organisation per month.** Duplicates, declines and "works as designed" earn nothing. Same amount for bugs and features. | Agreed abuse rules. |

## 2. Architecture

```
customer ──► /feedback (Offerte-AI) ──► AI triage + dedupe ──► GitHub issue [triage] ──► PM agent
                                                                                        │ story, files, priority
                                                                                        ▼
                     stijn approves / declines / marks manual  ◄──── admin page ◄── [needs-approval]
                                 │ approved
                                 ▼
          developer agent ──► branch + PR ──► test agent (security, privacy, functionality) ──► CI
                 ▲                                                                            │ green
                 │ findings (max 2 loops)                                                     ▼
                 └──────────────────────── E2E agent (Playwright) ◄── deploy PR to staging ◄──┘
                                                       │ e2e-passed
                                                       ▼
                        stijn reviews + merges ──► main ──► 14:00 release ──► production
                                                                               │ webhook "released"
                                                                               ▼
                                     Offerte-AI: mail reporter + followers, apply €20 Stripe credit
```

Every label transition in GitHub is mirrored to the product app by a signed webhook (reuse of the existing HMAC
webhook code in `app/integrations/events.py`, in the other direction). The factory knows nothing about
customers, money or Dutch mail texts; the product knows nothing about branches or agents.

### 2.1 Product side (in offerte-ai)

New module `app/feedback/` plus templates:

- `FeedbackRequest` table: org_id, github_issue, kind (bug|feature), title, status (mirrors label), eligible
  (bool), credit_status (none|pending|applied|refused), created_at.
- `FeedbackFollower` table: org_id, github_issue (for upvotes and release notifications).
- `Credit` table: org_id, amount_cents, reason, stripe_balance_txn_id, created_at. Mirror of Stripe.
- Routes: `GET/POST /feedback` (form + triage chat, reuses `AgentConversation` storage), `GET /backlog`
  (public, grouped by status, upvote button), `GET /beheer/feedback` (admin: approve, decline with reason,
  mark manual, see cost per issue), `POST /webhooks/factory` (signed, label transitions and `released`).
- Triage prompt outputs **structured JSON only** (kind, summary, steps, expected, actual, duplicate_of). The
  raw customer text is stored but never forwarded to the developer agent.
- Mails via the existing outbox: received, duplicate, approved, declined, released (+ credit), manual.
- Top-bar button "Idee of bug melden".

### 2.2 Factory side (new repo `agent-factory`)

```
agent-factory/
  .github/workflows/
    pm.yml          reusable: on label `triage`
    develop.yml     reusable: on label `approved`, and on comment `factory:retry`
    test.yml        reusable: on PR opened by the factory
    e2e.yml         reusable: on label `staging` after CI green
    release.yml     reusable: cron
    notify.yml      reusable: label change -> signed webhook to product
  skills/
    pm/SKILL.md, developer/SKILL.md, tester/SKILL.md, e2e/SKILL.md
  action/           composite action: run Claude Code with a skill, cost cap, structured output
  dashboard/        static page built by a workflow: per issue stage durations, token cost, outcome
  factory.schema.json
```

Each product repo adds `factory.yaml`:

```yaml
project: offerte-ai
stack: python
install: pip install -e ".[dev]"
test: pytest -q
lint: ruff check .
admin_github: SG87
staging_url: https://offerte-ai-staging.azurewebsites.net
e2e_seed: python scripts/seed_staging.py
webhook_url: https://app.offerte-ai.be/webhooks/factory
cost_cap_eur: 15
max_fix_loops: 2
release:
  weekdays_only: true
  hour_local: 14
  timezone: Europe/Brussels
```

and a 15-line `.github/workflows/factory.yml` that calls the reusable workflows.

Agent permissions: `contents: write` on non-protected branches only, `pull-requests: write`, `issues: write`.
No Azure credentials, no production secrets. Branch protection on `main`: PR required, CI green required,
review by SG87 required, agents cannot merge.

### 2.3 Dashboard

Built by `dashboard/` in the factory: one page per product with a table per issue (stage timestamps, loops
used, tokens and € per stage, final outcome) and totals per week. Data comes from workflow run artifacts
written by the composite action. Published to GitHub Pages of the factory repo, and linked from the admin
page in the product.

## 3. Phases and tasks

### Phase 1: production foundation (offerte-ai only, no agents)

1. `Dockerfile` (python:3.13-slim, uvicorn, non-root), `.dockerignore`.
2. GitHub Actions `ci.yml`: ruff + pytest on every PR and on `main`.
3. Postgres support: `OFFERTE_DATABASE_URL` postgres driver (`psycopg[binary]`), Alembic init with a
   baseline migration generated from the current models, remove the ad-hoc column adder. Tests keep SQLite.
4. Storage abstraction for files, Blob implementation (`azure-storage-blob`), local implementation for dev.
5. Azure resources (Bicep or `az` script in `infra/`): resource group, ACR, Postgres Flexible Server
   (staging + prod databases), Storage account, two App Services (staging, prod), Key Vault for secrets,
   OIDC federated credential for GitHub.
6. `deploy.yml`: build image, push to ACR, run `alembic upgrade head`, deploy to staging on push to `main`;
   production deploy only from `release.yml`.
7. Admin flag via `OFFERTE_ADMIN_EMAILS`; an `/beheer` landing page.
8. Health endpoint, structured logging, Application Insights.

Deliverable: current app running on Azure staging and production from `main`.

### Phase 2: billing

1. Stripe account, product + price, `stripe` SDK.
2. `Organisation.stripe_customer_id`, subscription status; checkout on onboarding or on `/account`.
3. Webhook `/webhooks/stripe` (invoice.paid, customer.subscription.updated).
4. `Credit` table and a service `grant_credit(org, 2000, reason)` that writes a Stripe customer balance
   transaction and mirrors it locally. Enforces the 3-per-month cap.
5. Account page shows plan, next invoice and open credits.

### Phase 3: feedback portal

Everything in 2.1 except the `released` handling. Issues are created in GitHub by the app with label `triage`
via a GitHub App installation token. Backlog page live. Admin gate live (sets labels through the GitHub API).

### Phase 4: factory repo with the PM agent only

1. Repo, composite action, cost cap, structured output, `notify.yml`, `pm.yml`.
2. `factory.yaml` and `factory.yml` in offerte-ai.
3. Run for at least a week on real requests; tune the PM skill until stories need no rewriting.

### Phase 5: developer and test agents

`develop.yml`, `test.yml`, bounded retry when tests fail, PR template with story link and checklist.
You review and merge by hand.

### Phase 6: staging per PR and E2E agent

1. `deploy.yml` gains a PR mode: on label `staging` it deploys the PR branch to the staging app (serialised,
   one PR at a time; the label is removed from the previous PR).
2. `scripts/seed_staging.py` creates the test organisation and sample projects.
3. `e2e.yml`: Playwright + Claude Code with the `e2e` skill, structured findings comment, feedback loop into
   `develop.yml` with the 2-loop cap.

### Phase 7: release and credits

`release.yml`, `released` webhook handling in the product (mails + credit), dashboard.

### Phase 8: second product

Onboard a second repo. Only then decide whether to extract `app/feedback/` into a pip package.

## 4. Open items

- Second product and its stack (affects whether the composite action needs a Node or other toolchain).
- Public backlog shows customer-submitted titles; the triage step rewrites titles and strips names,
  addresses and amounts before the issue is created. Confirm this is acceptable under the privacy policy.
- Stripe vs Adyen: Stripe chosen for customer balance and Customer Portal. Revisit only if Adyen pricing
  matters at volume.
