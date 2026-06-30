# Test Plan & Deployment Walkthrough — Hey Mr. Postman

> A step-by-step guide to standing up a **real test deployment** on a VPS,
> wiring the aggregation (hub) mailbox, and running the agent end-to-end in
> **shadow mode** (`draft_only`) against live mail without ever auto-sending.
>
> This document is generic (uses `example.com` / `brand-a` placeholders). Your
> real hosts, mailboxes, and secrets live only in gitignored local files — see
> [§9 Data security](#9-data-security--what-never-leaves-the-box).

---

## 0. Goals & guardrails

**What we're testing:** the full pipeline against *real* forwarded mail —
ingest → prefilter → classify → enrich → draft → approval queue → send-on-approval
→ folder move + audit — plus the SLA sweep and digest.

**Non-negotiable guardrails for the whole test:**

| Guardrail | Mechanism |
|-----------|-----------|
| Never auto-sends | `autonomy.mode: draft_only` (default) |
| Hard stop available at all times | `KILL_SWITCH=1` env var forces `draft_only` regardless of config |
| Sends only ever go to the real customer | only via explicit `approve <id>` while in `draft_only` |
| No live data reaches GitHub | `.env`, real `config/scenarios/*.yaml`, `data/`, `kb/`, `Docs/` are gitignored (see §9) |
| Idempotent / no loops | `messages.message_id UNIQUE` + INBOX membership; prefilter drops lists/auto-replies |

**Exit criteria to graduate out of the test** are in [§8](#8-graduation-criteria).

---

## 1. Test topology

```
  brand-a site (cPanel)  ──contact@/support@ forward──┐
  brand-b site (cPanel)  ──contact@/support@ forward──┼──►  hub@example.com   (aggregation mailbox)
  brand-c site (cPanel)  ──...────────────────────────┘            │  IMAP read (993, SSL)
                                                                    ▼
                                                          ┌───────────────────┐
                                                          │  VPS: mail-agent   │  draft_only
                                                          │  poll loop (systemd)│  → approval queue (SQLite)
                                                          └───────────────────┘
                                                                    │  SMTP send (465) — ONLY on approve
                                                    auth as the matched brand identity ─┘
```

Two safe ways to test, in order of increasing realism:

- **T0 — offline fixtures (no network):** `pytest` + `seed_test_emails` against a
  throwaway mailbox. Proves the code without touching real customers.
- **T1 — live shadow:** real forwards into the hub, agent in `draft_only`. The
  agent drafts replies to real mail but sends **nothing** until you approve.

Do **T0 fully** before T1.

---

## 2. Provision the VPS

Assumes Ubuntu 22.04/24.04. Any VPS with Python 3.11+ works.

```bash
# as root, then create an unprivileged service user
adduser --disabled-password --gecos "" mailagent
apt update && apt install -y python3 python3-venv python3-pip git
python3 --version           # must be >= 3.11
```

Work as the service user from here on:

```bash
su - mailagent
```

> **Firewall:** the agent makes only *outbound* connections (IMAP 993, SMTP 465,
> HTTPS to the Anthropic API). It needs **no inbound ports**. Keep the VPS
> firewall default-deny inbound except SSH.

---

## 3. Install the agent

```bash
# clone YOUR deployment copy (see §9 for which repo this should be)
git clone <your-deployment-remote-or-local-path> hey-mr-postman
cd hey-mr-postman

python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"      # dev extras so you can run pytest on the box

pytest -q                    # T0 gate #1 — all tests must pass before going further
```

If `pytest` fails, stop and fix before connecting anything live.

---

## 4. Set up the aggregation (hub) email account

This is the heart of the wiring. Do it **before** configuring the agent.

### 4.1 Create the hub mailbox

In cPanel on the hub domain (e.g. `example.com`):

1. **Email Accounts → Create** → `hub@example.com`. Give it a **large quota**
   (a full hub mailbox *bounces forwards* — see warning below).
2. Note the **IMAP** settings: host (`mail.example.com`), port `993`, SSL on.

### 4.2 Point each brand's contact addresses at the hub

On **each** brand site's cPanel:

1. **Email → Forwarders → Add Forwarder.**
2. Forward `contact@brand-a.example.com` (and `support@`, `admin@` as used) →
   `hub@example.com`.
3. Repeat for every brand/site you want the agent to cover.

> **Keep the original mailbox too (recommended during test):** set the forwarder
> to *forward a copy* if cPanel offers it, or use a mailbox + forwarder, so you
> retain an independent record of inbound mail outside the agent while testing.

### 4.3 Create the per-brand SENDING identities

The agent replies authenticated **as the brand mailbox**, through **that brand's**
SMTP server (not the hub's). For each brand you'll approve replies for:

1. Ensure the sending mailbox exists (e.g. `support@brand-a.example.com`).
2. Note its SMTP host (`mail.brand-a.example.com`), port `465`, SSL.

### 4.4 Deliverability — do this or replies land in spam

For **every sending domain**, configure in cPanel → **Email Deliverability**:

- **SPF** — include the brand mail server.
- **DKIM** — enable; publish the key.
- **DMARC** — at least `p=none` during test so you get reports without blocking.

Sending brand mail authenticated through the wrong server **fails DMARC**. Verify
each domain shows all-green in cPanel's Email Deliverability before T1.

### 4.5 Verify the forwarding path manually

Before the agent touches anything, prove the plumbing:

1. From an external account, email `contact@brand-a.example.com`.
2. Confirm it arrives in `hub@example.com` within a minute.
3. Log into the hub via any IMAP client to confirm credentials + folder access.

---

## 5. Configure the agent

### 5.1 Secrets (`.env` — never committed)

```bash
cp .env.example .env
```

Fill in:

```bash
ANTHROPIC_API_KEY=sk-ant-...
HUB_IMAP_PASSWORD=...                 # hub@example.com IMAP password
BRAND_A_SUPPORT_PASSWORD=...          # support@brand-a.example.com SMTP password
BRAND_B_CONTACT_PASSWORD=...
KILL_SWITCH=                          # leave empty for now; set to 1 to force draft_only
```

### 5.2 Scenario file (gitignored — your real hosts)

```bash
cp config/scenarios/example.yaml config/scenarios/mytest.yaml
```

Edit `mytest.yaml` for your deployment. Key fields (full reference in
`example.yaml`):

- `transport.imap` — hub host/port/username + `password_env: HUB_IMAP_PASSWORD`.
- `transport.imap.ingest_mode` — keep `poll` (simplest) for the test.
- `sending_identities[]` — one per brand: `match_to` (the incoming `to_addr`
  that picks this identity), host, port `465`, username, `password_env`.
- `brands{}` — brand key → `kb_path` + `voice`. **Brand inference matches the
  brand key as a substring of the recipient domain** (e.g. `brand-a` in
  `support@brand-a.example.com`). ⚠️ If your real brand keys won't literally
  appear in the domains, this won't match — confirm during §7.2 and, if needed,
  rely on the explicit `sending_identities[].match_to` mapping.
- `autonomy.mode` — **leave `draft_only`** for the entire test.
- `storage.*` — leave under `data/` (gitignored).

> All commands below take `--scenario config/scenarios/mytest.yaml`. Without it
> they default to the public `example.yaml`, which won't connect to your hub.

### 5.3 Seed the knowledge base

```bash
mkdir -p kb/brand-a kb/brand-b
# drop brand FAQ / policy / tone docs as .md or .txt into each kb_path
python -m scripts.ingest_kb --brand brand-a --scenario config/scenarios/mytest.yaml
python -m scripts.ingest_kb --brand brand-b --scenario config/scenarios/mytest.yaml
```

---

## 6. T0 — offline smoke test (no live mail)

Run against a **throwaway** mailbox or the fixtures so no real customer is
involved.

```bash
# Option A: pure fixtures through the pipeline (already covered by tests)
pytest -q

# Option B: APPEND sample .eml fixtures to a TEST mailbox, then process once
python -m scripts.seed_test_emails --scenario config/scenarios/mytest.yaml
python -m scripts.run_agent once   --scenario config/scenarios/mytest.yaml
python -m scripts.run_agent approvals --scenario config/scenarios/mytest.yaml
```

**Expected:** drafts appear in the approval queue; nothing is sent; processed
fixtures move out of INBOX. Inspect a draft, then exercise the queue verbs:

```bash
python -m scripts.run_agent approve  <id> --scenario config/scenarios/mytest.yaml   # sends as the brand identity
python -m scripts.run_agent edit     <id> "tweaked body" --scenario config/scenarios/mytest.yaml
python -m scripts.run_agent discard  <id> --scenario config/scenarios/mytest.yaml
python -m scripts.run_agent escalate <id> --scenario config/scenarios/mytest.yaml
```

⚠️ Only run `approve` in T0 against an address **you control** — it really sends.

---

## 7. T1 — live shadow run

### 7.1 One controlled message, end to end

1. Confirm `autonomy.mode: draft_only` and `KILL_SWITCH=` (empty).
2. From an external account **you control**, email
   `contact@brand-a.example.com` with a realistic support question.
3. Run a single pass and inspect:

```bash
python -m scripts.run_agent once     --scenario config/scenarios/mytest.yaml
python -m scripts.run_agent approvals --scenario config/scenarios/mytest.yaml
```

### 7.2 Verification checklist for that message

- [ ] **`true_sender`** resolved to *your* address, not the website's system
      address (Reply-To → body regex → From). This is the linchpin rule.
- [ ] **Brand inferred correctly** (drafted in brand-a's voice, queued against
      the brand-a identity). If wrong, revisit §5.2 brand matching.
- [ ] **Draft quality** — grounded in the KB you seeded; on-voice.
- [ ] **Nothing was sent** (it's `draft_only`).
- [ ] **Idempotency** — run `once` again; the same message is **not** reprocessed.
- [ ] **Folder move + audit** — message left INBOX; an `audit_log` row exists.

### 7.3 Approve a reply for real

Pick the queued draft and approve it:

```bash
python -m scripts.run_agent approve <id> --scenario config/scenarios/mytest.yaml
```

- [ ] Reply arrives at your external address **from the brand identity**
      (`support@brand-a.example.com`), not the hub.
- [ ] Passes SPF/DKIM/DMARC (check the received headers / not in spam).
- [ ] Thread marked `Replied`; audit row written.

### 7.4 Run the poll loop as a service

Once single passes look right, run continuously. Create a systemd unit
(`/etc/systemd/system/mail-agent.service`, as root):

```ini
[Unit]
Description=Hey Mr. Postman email agent (shadow mode)
After=network-online.target
Wants=network-online.target

[Service]
User=mailagent
WorkingDirectory=/home/mailagent/hey-mr-postman
EnvironmentFile=/home/mailagent/hey-mr-postman/.env
ExecStart=/home/mailagent/hey-mr-postman/.venv/bin/python -m scripts.run_agent run --scenario config/scenarios/mytest.yaml
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mail-agent
journalctl -u mail-agent -f        # watch ingest logs
```

The poll loop also runs the SLA sweep each iteration.

---

## 8. SLA follow-up & digest

```bash
python -m scripts.run_agent sla    --scenario config/scenarios/mytest.yaml   # nudge drafts past sla.follow_up_hours
python -m scripts.run_agent digest --scenario config/scenarios/mytest.yaml   # activity + queue summary over digest.window_hours
```

- [ ] A draft left unactioned past `sla.follow_up_hours` is flagged once
      (audit `sla_followup`, thread marked `overdue`).
- [ ] Digest reflects counts of processed / queued / sent / escalated.

---

## 8. Graduation criteria

Stay in `draft_only` until **all** hold over a meaningful sample (e.g. ≥ 1 week
and ≥ N real messages per brand):

- [ ] `true_sender` correct on 100% of sampled messages (esp. forwarded forms).
- [ ] Brand inference correct on 100% of sampled messages.
- [ ] Zero loop/auto-reply/list messages drafted (prefilter holds).
- [ ] Draft quality acceptable to the human approver with minimal edits.
- [ ] All sending domains pass SPF/DKIM/DMARC; approved sends not flagged spam.
- [ ] Hub mailbox stays well under quota; no bounced forwards.

**Then** graduate in steps, re-validating at each: `draft_only` → `approval`
(every send still needs one human decision) → `auto` (only after tuning
`auto_send_allowlist` + `confidence_threshold`; `billing|legal|refund` and
negative-sentiment/low-confidence always stay human-gated).

---

## 9. Data security — what never leaves the box

The repo is already built so **live data never enters git**. Everything that
could identify a customer or expose a secret is gitignored:

| Path | Contains | Status |
|------|----------|--------|
| `.env` | API key, mailbox passwords | gitignored |
| `config/scenarios/*.yaml` (except `example.yaml`) | real hosts/mailboxes | gitignored |
| `data/` (`*.db`, Chroma store), attachments | **real customer emails + CRM** | gitignored |
| `kb/` | brand knowledge docs | gitignored |
| `Docs/` | build brief (names real domain) | gitignored |

**Verify before any commit on the VPS:**

```bash
git status --porcelain        # none of the paths above should appear
git check-ignore -v .env config/scenarios/mytest.yaml data kb   # should all report a match
```

> See the repo-strategy note from setup for how the deployment copy is version
> controlled (local-only vs. private remote) and the pre-push safety guard. The
> rule that matters: **the SQLite DB under `data/` holds real customer mail —
> treat the whole `data/` dir as production PII and never push it anywhere.**

---

## 10. Rollback / panic

- **Stop sending instantly:** set `KILL_SWITCH=1` in `.env` and restart the
  service — forces `draft_only` regardless of config.
- **Stop the agent:** `sudo systemctl stop mail-agent`.
- **Undo forwarders:** remove the cPanel forwarders to detach brands from the hub.
- Nothing is destructive: deletes are soft (`\Deleted` → `Trash/`), hard expunge
  only after `retention.trash_grace_days`.