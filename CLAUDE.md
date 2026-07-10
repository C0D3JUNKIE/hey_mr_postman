# Hey Mr. Postman — Email Agent · Project Memory

> This file is the durable project context. It travels with the repo, so when
> you reopen the project at a new location, this is the handoff record. Keep it
> current as the project evolves.

## What this is

An autonomous email agent that monitors one aggregation mailbox receiving
forwarded mail from the `contact@` / `support@` / `admin@` addresses of several
cPanel-hosted sites. Per message it: pre-filters junk → classifies → enriches
(CRM history + per-brand KB) → drafts a brand-voiced reply → routes through a
human approval gate → sends from the correct per-brand identity → tracks the
thread. Transport is **IMAP + SMTP** (cPanel), not a vendor API.

Built **ports & adapters** (hexagonal): the core is transport-agnostic so the
IMAP/SMTP transport can later be swapped (Gmail/Graph) without touching
classification, RAG, or approval logic. **Ships in shadow mode** (`draft_only` —
drafts everything, sends nothing) until graduated.

The full build brief is `Docs/email-agent-build-spec.md` (gitignored, local only).

## Architecture & dependency rule

`src/agent/core/` is pure domain logic and imports **only** ports + models +
`langgraph` (orchestration) + config. It must **never** import
`imaplib`/`smtplib`/`chromadb` — those live only in adapters. There's a test/grep
that enforces this. Dependencies point inward; concrete adapters are wired to the
core in the composition root (`scripts/run_agent.py`), selected by config.

- `core/` — `models`, `pipeline` (LangGraph), `prefilter`, `classify`, `enrich`, `draft`, `route`
- `ports/` — 4 Protocols: `TransportPort`, `CRMPort`, `KnowledgePort`, `NotificationPort`
- `adapters/` — one each: `transport/imap_smtp`, `crm/internal_db`, `knowledge/chroma_kb`, `notification/web_queue`
- `store/` — `schema.sql` (SQLite, Postgres-compatible DDL), `db.py`, `repos.py` (messages/threads/audit)
- `config.py` — pydantic scenario loader · `llm.py` — Anthropic wrappers · `maintenance.py` — lifecycle/GDPR seams

## Build status (spec §14 phases)

- ✅ **Phase 0–5 implemented and tested** (41 tests passing): scaffold, IMAP/SMTP
  transport with forwarded-sender extraction, prefilter, Haiku classify,
  SQLite persistence + internal CRM, Chroma KB + Sonnet drafting, LangGraph
  route + approval queue, send-on-approval + finalize + audit.
- ✅ **Phase 6 (lifecycle) complete** (one intentional seam remains):
  - **SLA follow-up timer + daily digest** (`maintenance.py`):
    `run_sla_followups` nudges approvals past `sla.follow_up_hours` once each
    (audit `sla_followup` + marks thread `overdue`/sets `sla_due`); `build_digest`
    /`render_digest` summarize audit activity + queue state over `digest.window_hours`.
    Surfaced as `run_agent sla` / `run_agent digest`; the sweep also runs each poll
    iteration (v1's "background" timer — no separate scheduler). Config: `sla:` and
    `digest:` blocks. Digest renders to stdout/log (email/Slack routing is a later seam).
  - **Attachment offload**: transport offloads blobs to the object store
    (`storage.attachments_path`) and the pipeline records refs in the new
    `attachments` table — done in `_enrich`, so only post-prefilter mail is stored.
    Gated by `retention.offload_attachments`.
  - **Trash retention + quota warning**: `expunge_trash` hard-deletes Trash/ items
    older than `retention.trash_grace_days` (transport `expunge_older_than`);
    `warn_on_quota` logs when mailbox usage crosses the threshold. Both run in a
    throttled daily housekeeping sweep on the poll loop and via `run_agent maintenance`.
  - ⏳ **One intentional seam (spec §16, do not build in v1)**: `purge_contact`
    (GDPR erasure fan-out) stays inert — destructive cross-store deletion is
    one-way and must not ship half-built.

## Key design decisions (don't re-litigate)

- **Approval gate = durable pause, not in-memory interrupt.** The route node
  enqueues to the DB and stops; resumption is a separate path
  (`AgentPipeline.process_approval`). A human may take hours — longer than a
  LangGraph in-memory interrupt should hold a process. Keeps the v1 loop
  synchronous and crash-safe. (Revisit only if a checkpointer-based interrupt is
  explicitly wanted.)
- **`true_sender` resolution** (the linchpin, spec §6): prefer `Reply-To`, then a
  body-regex fallback for form-mailer payloads, then `From`. Forwarded
  contact-form mail rewrites `From` to the website's own system.
- **Brand inference** matches a brand key as a substring of the recipient domain
  (e.g. `brand-a` in `support@brand-a.example.com`). If real brand keys won't
  appear literally in the domains, add an explicit `match_to → brand` mapping in
  config instead. **(Open item to confirm before real deployment.)**
- **Models are config values** (`models.triage` Haiku / `models.draft` Sonnet),
  never hardcoded.

## Public-repo / privacy setup

This repo is meant to be **public**, scrubbed of identifying info:
- Committed default config is generic: `config/scenarios/example.yaml`
  (`example.com`, `brand-a`/`brand-b`).
- **Real/private scenarios are gitignored**: `config/scenarios/*.yaml` with
  `!example.yaml`. Keep your real deployment config (real hosts/mailboxes) as
  e.g. `config/scenarios/<yours>.yaml` — it stays local.
- `Docs/` is gitignored (the build brief names the real domain).
- All commands default to `example.yaml`; pass `--scenario path/to/yours.yaml`
  (or `SCENARIO=` env for the exim pipe) for real runs.
- Never commit `.env`. Secrets are referenced by env-var *name* (`*_env`) in
  config; values live only in `.env` / the environment.

## Commands

```bash
# Setup (recreate the venv after moving — see note below)
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # fill in secrets

pytest -q                        # 41 tests

python -m scripts.run_agent once            # single ingest pass
python -m scripts.run_agent run             # poll loop
python -m scripts.run_agent approvals       # list pending drafts
python -m scripts.run_agent approve <id>    # send as brand identity
python -m scripts.run_agent edit/discard/escalate <id>
python -m scripts.run_agent sla             # nudge approvals past SLA (also runs in poll loop)
python -m scripts.run_agent digest          # print activity + queue summary
python -m scripts.run_agent maintenance     # expunge aged Trash + quota warn (also daily in poll loop)
# KB refresh (scrape → review → ingest). Sources declared per brand (brands.<b>.sources).
python -m scripts.scrape_site --brand brand-a --prune   # fetch config sources → kb/<brand>/;
                                            # reports new/CHANGED pages, prunes vanished ones
python -m scripts.ingest_kb --brand brand-a --replace   # authoritative rebuild (no stale chunks)
#   ad-hoc (no config): scrape_site --brand brand-a --auth-user U --auth-pass-env PW_ENV --crawl <url>
python -m scripts.seed_test_emails          # APPEND fixtures to a TEST mailbox

# deployment (see TEST_PLAN.md + DEPLOY_IONOS.md)
scripts/bootstrap-deploy-repo.sh            # local-only (no-remote) repo for real config
scripts/install-git-hooks.sh .              # commit/push guards: no secrets/live data
scripts/update.sh                           # pull from GitHub (ff-only) + test gate + restart
```

## Environment notes

- **Python 3.11+** (developed/tested on 3.14). All deps — including `chromadb`
  and `langgraph` — install cleanly on 3.14.
- **The `.venv/` is not portable** and is gitignored. After moving the project,
  recreate it: `python -m venv .venv && pip install -e ".[dev]"`.
- **Avoid iCloud/Dropbox-synced dirs for this project** — SQLite `data/*.db`,
  the Chroma store, and the venv don't tolerate sync races. (This move to a local
  dir is exactly why.) `data/` and `kb/` are gitignored.

## Stack

Python 3.11+ · `imap-tools`/stdlib `imaplib`+`smtplib` · `langgraph` ·
`anthropic` (Haiku triage / Sonnet drafting) · `chromadb` (local KB) · SQLite ·
`pydantic` · `python-dotenv`.