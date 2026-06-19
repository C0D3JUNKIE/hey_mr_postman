# Hey Mr. Postman — Email Agent

An autonomous agent that monitors one aggregation mailbox (e.g. `hub@example.com`)
receiving forwarded mail from the `contact@` / `support@` / `admin@` addresses of
several cPanel-hosted sites. For each message it pre-filters junk, classifies,
enriches with CRM history + a per-brand knowledge base, drafts a brand-voiced
reply, routes it through a human approval gate, sends from the correct per-brand
identity, and tracks follow-up.

Built **ports & adapters** (hexagonal): the core is transport-agnostic, so the
cPanel IMAP/SMTP transport can later be swapped for Gmail/Graph without touching
classification, RAG, or approval logic.

> **Ships in shadow mode.** Default autonomy is `draft_only` — it drafts
> everything and sends nothing until you graduate it (see *Autonomy*).

---

## Architecture

```
core/      transport-agnostic domain logic — imports ONLY ports + models
  models.py      domain types
  pipeline.py    LangGraph orchestration (parse→prefilter→classify→enrich→draft→route→finalize)
  prefilter.py   rule-based junk/loop/auto-reply drop (pre-LLM)
  classify.py    Haiku triage → Classification (strict JSON)
  enrich.py      CRM history + KB retrieval
  draft.py       Sonnet brand-voiced drafting
  route.py       autonomy decision: send | approve | escalate
ports/     four Protocols: Transport, CRM, Knowledge, Notification
adapters/  one implementation each (v1):
  transport/imap_smtp.py     cPanel IMAP read + SMTP send
  crm/internal_db.py         contacts + interactions in SQLite
  knowledge/chroma_kb.py     local Chroma per-brand KB
  notification/web_queue.py  approval queue (CLI surface)
store/     SQLite schema + repos (Postgres-compatible DDL)
```

**Dependency rule:** dependencies point inward. The core never imports
`imaplib`, `smtplib`, or `chromadb` — those live only in adapters. Concrete
adapters are wired to the core at startup in `scripts/run_agent.py` (the
composition root), selected by config.

> **New to the codebase?** Read [`WALKTHROUGH.md`](WALKTHROUGH.md) — a
> phase-by-phase tour of what each file does, the patterns it uses, and the key
> API calls (IMAP/SMTP, the Anthropic Messages API, Chroma, LangGraph).

---

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .                 # or: pip install -e ".[dev]"
cp .env.example .env             # fill in secrets — never commit .env
```

Copy `config/scenarios/example.yaml` to your own scenario file and edit it for
your hosts, mailboxes, brands, and autonomy. Every secret is referenced by the
*name* of an env var (`*_env`); the value lives only in `.env` / the environment.
Real scenario files are gitignored (only `example.yaml` is published) — keep your
deployment config local.

### Seed the knowledge base

```bash
# put brand docs (.md/.txt) under the brand's kb_path, then:
python -m scripts.ingest_kb --brand siteA
```

---

## Running

All commands default to `config/scenarios/example.yaml`; point at your own
private scenario with `--scenario path/to/your.yaml`.

```bash
python -m scripts.run_agent once        # single ingest pass
python -m scripts.run_agent run         # poll loop (default)

# approval queue (nothing auto-sends in draft_only):
python -m scripts.run_agent approvals           # list pending drafts
python -m scripts.run_agent approve  <id>       # send as the brand identity
python -m scripts.run_agent edit     <id> "..." # edit body, then send
python -m scripts.run_agent discard  <id>
python -m scripts.run_agent escalate <id>
```

Ingest modes (config `transport.imap.ingest_mode`): `poll` (default), `idle`
(IMAP IDLE), or `pipe` (cPanel Exim pipe-to-program via
`scripts/exim_pipe_ingest.py`).

To exercise end-to-end against a **throwaway** test mailbox:

```bash
python -m scripts.seed_test_emails      # APPENDs tests/fixtures/*.eml to INBOX
python -m scripts.run_agent once
```

---

## Autonomy & safety

- **`draft_only`** (default) → drafts everything, sends nothing.
- **`approval`** → every send needs one human decision.
- **`auto`** → per-category auto-send, gated by an allowlist + confidence threshold.

Always routed to a human: negative sentiment, low confidence, or
`billing | legal | refund`. A global **kill switch** (`KILL_SWITCH` env var)
forces `draft_only` regardless of config.

Other non-negotiables (enforced in code): loop prevention (never reply to
no-reply / lists / auto-responders), idempotency (`messages.message_id` +
INBOX membership), pre-filter before any LLM call, secrets only via env, and an
`audit_log` row for every send / move / escalate.

---

## ⚠️ Deliverability — read before going live

Each **sending domain** must have **SPF, DKIM, and DMARC** configured
(cPanel → *Email Deliverability*). The agent authenticates as the matched brand
mailbox and sends through **that brand's** server (SMTP_SSL 465). Sending brand
mail authenticated through the wrong server **fails DMARC and lands in spam**.

The aggregation hub also forwards mail in: keep the hub mailbox under quota — a
full mailbox **bounces forwards**. The agent logs mailbox usage and warns at a
threshold (`maintenance.warn_on_quota`).

---

## Mailbox lifecycle

INBOX is a **queue, not an archive**. After handling, a message is moved out of
INBOX into a status folder — `Replied/`, `Awaiting-Approval/`, `Escalated/`, or
`No-Action/`. A message not in INBOX is never reprocessed. Soft delete flags
`\Deleted` → `Trash/`; hard `EXPUNGE` only after `retention.trash_grace_days`.

GDPR erasure has a single seam — `maintenance.purge_contact(email)` — that will
fan out to SQLite, Chroma, and the attachment store. It is a **stub** in v1
(deletion is one-way; not shipped half-built).

---

## Tests

```bash
pytest -q
```

Covers forwarded-sender extraction (`Reply-To` / body fallback over the
forwarder's `From`), the pre-filter rules, the routing decision table, and a
full pipeline run with stub adapters (queue in `draft_only`, send-on-approval
from the matched brand identity, idempotent re-runs).

---

## Stack

Python 3.11+ · `imap-tools`/stdlib `imaplib`+`smtplib` (transport) ·
`langgraph` (orchestration) · `anthropic` (Haiku triage / Sonnet drafting) ·
`chromadb` (local KB) · SQLite (Postgres-compatible) · `pydantic` (config).
Models are config values in `config/scenarios/*.yaml`, never hardcoded.