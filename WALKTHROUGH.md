# Code Walkthrough — Hey Mr. Postman

A guided, phase-by-phase tour of the codebase for someone seeing it for the
first time. Read it top-to-bottom with the files open beside you. Each phase
points at the files that matter, explains the **pattern** being used, and calls
out the **important API calls** (IMAP/SMTP, the Anthropic Messages API, Chroma,
LangGraph, SQLite) so you can recognise them in the wild.

If you only read one thing first, read the two big ideas below — almost every
design decision falls out of them.

---

## The two big ideas

### 1. Ports & adapters (hexagonal architecture)

The program is split into three rings:

```
        ┌──────────────────────────────────────────────┐
        │  adapters/   (the messy outside world)        │
        │   imap_smtp · chroma_kb · internal_db · queue  │
        │   ┌──────────────────────────────────────┐    │
        │   │  ports/   (Protocols — the contracts) │    │
        │   │   ┌──────────────────────────────┐    │    │
        │   │   │  core/   (pure domain logic)  │    │    │
        │   │   │   models · pipeline · classify │    │    │
        │   │   │   enrich · draft · route       │    │    │
        │   │   └──────────────────────────────┘    │    │
        │   └──────────────────────────────────────┘    │
        └──────────────────────────────────────────────┘
              dependencies only ever point inward →
```

* **`core/`** is pure logic. Given an email, decide what to do with it. It never
  opens a socket, never imports `imaplib`/`smtplib`/`chromadb`, never talks to a
  database directly. It only knows about *ports* and *models*.
* **`ports/`** are `typing.Protocol` classes — interfaces with no
  implementation. "Something that can `read_new()` and `send()`" is a
  `TransportPort`; the core depends on that shape, not on IMAP.
* **`adapters/`** are the concrete implementations that touch the real world.
  `ImapSmtpTransport` *satisfies* `TransportPort`. Swap it for a `GmailTransport`
  later and the core never notices.

**Why care?** Every piece of network/LLM/database I/O is at the edge, so the
interesting logic (classification, routing, the approval gate) is testable with
fakes and no network. See `tests/test_pipeline.py` — it runs the whole flow with
stub adapters and zero live connections.

**The seam where it's wired together** is `scripts/run_agent.py` — the
"composition root." That's the *only* place that picks concrete adapters:

```python
# scripts/run_agent.py  — App.__init__
self.transport = _build_transport(config)        # ImapSmtpTransport
self.crm       = InternalDbCRM(self.db)
self.knowledge = ChromaKnowledge(config.storage.chroma_path)
self.notifier  = WebQueueNotifier(self.db)
self.pipeline  = AgentPipeline(config=config, transport=self.transport, ...)
```

This pattern is **dependency injection**: the pipeline is *handed* its
collaborators instead of constructing them. That's what makes the swap-and-test
story work.

### 2. Everything speaks in `models`

`core/models.py` defines the shared vocabulary — `Email`, `Classification`,
`Draft`, `Contact`, `PendingApproval`, etc. They're **pydantic** models, so:

* config loading, LLM JSON parsing, and DB rows all get validation for free;
* `confidence: float = Field(ge=0.0, le=1.0)` *can't* hold an out-of-range value;
* `model_dump(mode="json")` / `model_validate(...)` give you clean
  serialize/deserialize for persistence.

Every layer passes these objects around. The transport adapter turns raw bytes
*into* an `Email`; the core reasons *about* the `Email`; nothing invents its own
ad-hoc dict shape. **When you're lost, open `models.py`** — it's the map.

---

## How one email flows through the system

The whole job is a pipeline. One inbound message walks through these stages,
which line up with the phases below:

```
 read bytes        parse           prefilter        classify        enrich
 (transport)  →  (→ Email)   →   (drop junk?)  →   (Haiku)    →   (CRM + KB)
                                       │
                                       ▼ (junk → No-Action/, stop)
                                                                      │
                                                                      ▼
   finalize         route            draft
 (move + audit) ← (send/approve/ ← (Sonnet, grounded)
                   escalate)
```

`core/pipeline.py` encodes exactly this as a **LangGraph** state machine. Keep
that diagram in mind; the rest of this document zooms into each box.

---

## Phase 0 — Scaffold & contracts

> *What's here: the skeleton everything else hangs on — models, ports, config,
> the SQLite schema, and the composition root.*

**Files:** `core/models.py`, `ports/*.py`, `config.py`, `store/schema.sql`,
`store/db.py`, `scripts/run_agent.py`

### The ports (`ports/`)

Four tiny files, each one `Protocol`. This is the entire contract surface
between core and the outside world:

| Port | File | What it abstracts |
|------|------|-------------------|
| `TransportPort` | `ports/transport.py` | read/parse/send mail, move folders |
| `CRMPort` | `ports/crm.py` | contacts + interaction history |
| `KnowledgePort` | `ports/knowledge.py` | per-brand KB retrieval |
| `NotificationPort` | `ports/notification.py` | the human approval queue |

Note the pattern — a `Protocol` with `...` bodies and `@runtime_checkable`:

```python
@runtime_checkable
class TransportPort(Protocol):
    def read_new(self) -> list[RawMessage]: ...
    def parse(self, raw: RawMessage) -> Email: ...
    def send(self, reply: OutgoingEmail) -> None: ...
```

There is no inheritance. `ImapSmtpTransport` never says
`class ImapSmtpTransport(TransportPort)`. It just *has the right methods*
(structural typing / "duck typing with a type checker"). That keeps adapters
fully decoupled from the core.

### Config (`config.py`)

A deployment is one YAML file (`config/scenarios/example.yaml`) validated into a
`ScenarioConfig` pydantic tree. Three patterns worth noting:

* **Secrets by *name*, never by value.** Config holds `password_env:
  HUB_IMAP_PASSWORD` — the *name* of an env var. The actual password is resolved
  at adapter-construction time via `resolve_secret()`, which reads
  `os.environ` and **fails loudly** if unset. Nothing secret ever lands in a
  committed file.
* **Validation at the edge.** `@field_validator` rejects an illegal
  `ingest_mode` or `autonomy.mode` at load time, so the rest of the code can
  assume valid values.
* **The kill switch lives here.** `effective_mode()` returns `"draft_only"` if
  the `KILL_SWITCH` env var is truthy, *regardless* of config. This is the
  global "stop sending" lever, and it's read fresh every time.

### Store (`store/`)

* `schema.sql` — six tables (`contacts`, `threads`, `interactions`, `messages`,
  `approvals`, `audit_log`). Written **Postgres-compatible** (timestamps as
  TEXT, no SQLite-only types) so a future migration is trivial.
* `db.py` — a thin `Database` wrapper over stdlib `sqlite3`. No ORM. Note
  `conn.row_factory = sqlite3.Row` (rows behave like dicts) and the helpers
  `query_one` / `query_all` / `execute`. **All SQL uses `?` parameters** — never
  string formatting — so it's injection-safe.
* `new_id()` returns a `uuid4().hex`; `now_iso()` returns a UTC ISO-8601 string.

**Key idea to carry forward:** the `messages.message_id` column is `UNIQUE`.
That single constraint is the backbone of idempotency (Phase 5).

---

## Phase 1 — Transport: IMAP/SMTP + the `true_sender` linchpin

> *What's here: the one adapter allowed to touch the network. Turns raw bytes
> into a clean `Email`, and sends replies as the correct brand identity.*

**File:** `adapters/transport/imap_smtp.py`

This file is deliberately split into two parts:

### Part A — pure MIME parsing (the testable half)

Everything from `_first_addr` down to `parse_raw` is **module-level pure
functions over bytes**. No connection needed, so they're unit-tested directly
against `.eml` fixtures (`tests/test_parse.py`). This is a recurring trick:
*push the logic into pure functions, keep the I/O thin.*

**The linchpin — `resolve_true_sender()`.** Forwarded contact-form mail rewrites
the `From:` header to the *website's own system* (e.g. `wordpress@siteA.com`),
not the actual customer. Replying to `From:` would email the website back. So we
recover the real address in priority order:

```python
def resolve_true_sender(from_addr, reply_to, body_text):
    1. Reply-To       # forwarders set this to the real customer → wins
    2. body regex     # form-mailers embed "Email: jane@x.com" in the body
    3. From           # last resort
```

This is *the* most important domain rule in the project. `test_parse.py` proves
all three branches.

**MIME parsing uses the stdlib `email` package** with the modern API:

```python
msg = email.message_from_bytes(raw_bytes, policy=default_policy)
msg.get("From"); msg.get("Subject")          # headers
getaddresses([value])                          # parse "Name <addr>" → addr
parsedate_to_datetime(msg.get("Date"))        # RFC 2822 date → datetime
msg.walk()                                      # iterate MIME parts
part.get_content() / part.get_content_type()   # body text vs html vs attachment
```

`resolve_brand()` infers which brand an email belongs to by checking whether a
configured brand key (`brand-a`) appears in the recipient domain. (The README
flags this substring heuristic as something to confirm before a real deploy.)

### Part B — the adapter class (the I/O half)

`ImapSmtpTransport` is where `imaplib`/`smtplib` are actually allowed. Notable
patterns and the raw protocol calls:

* **Lazy, self-healing connection.** `_connect_imap()` reuses an existing
  connection if `conn.noop()` succeeds, else reconnects. Folders are auto-created
  idempotently (`conn.create(name)` swallowing "already exists").

* **Reading new mail** — the classic IMAP dance:
  ```python
  conn.select("INBOX")
  conn.uid("SEARCH", None, "UNSEEN")        # find unread UIDs
  conn.uid("FETCH", uid, "(BODY.PEEK[])")   # PEEK = fetch without marking \Seen
  ```
  Working by **UID** (not sequence number) is deliberate — UIDs are stable.

* **Sending a reply** — `send()` is where the **DMARC safety rule** lives. It
  looks up the `SendingIdentity` matching the original recipient and
  authenticates as *that brand's* server. If no identity matches, it
  **refuses to send** rather than send from the wrong server (which would fail
  DMARC and land in spam):
  ```python
  with smtplib.SMTP_SSL(identity.host, identity.port, context=ssl_ctx) as smtp:
      smtp.login(identity.username, password)
      smtp.send_message(msg)
  ```
  The reply sets `In-Reply-To` and `References` headers so it threads correctly
  in the customer's client.

* **Folder moves** — `move()` prefers `UID MOVE` (RFC 6851) and falls back to
  `COPY` + flag `\Deleted` + `expunge()` on older servers. `mark_processed()`
  sets `\Seen` as a backup idempotency marker.

---

## Phase 2 — Prefilter: drop junk *before* spending a token

> *What's here: cheap, rule-based filtering that runs before any LLM call.*

**File:** `core/prefilter.py`

A single pure function `prefilter(email) -> PrefilterResult`. It returns
`drop=True` with a reason, or `drop=False`. The pipeline moves dropped mail to
`No-Action/` and stops — no classify, no draft, **no API cost.**

The rules, in order, are all about **loop prevention** (a safety
non-negotiable — the agent must never get into a reply war with a robot):

1. Never reply to `no-reply` / `mailer-daemon` / `bounce` / `postmaster` style
   senders (substring match on the local-part).
2. `Auto-Submitted` header present and not `"no"` → automated (RFC 3834).
3. Mailing-list / bulk headers (`List-Id`, `List-Unsubscribe`,
   `Precedence: bulk`, …).
4. Subject heuristics: "out of office", "auto-reply", "delivery status
   notification", etc.
5. Empty body → nothing to act on.

**Pattern to notice:** the core stays transport-agnostic even though it needs
raw headers. The adapter captures the relevant headers
(`extract_raw_headers()`) and *passes them in* via `extra_headers=`; the core
never reaches back into MIME. That's the ports discipline holding even for a
small convenience.

---

## Phase 3 — Classify & enrich: triage, then gather context

> *What's here: the cheap LLM triage step, and the lookup of CRM history + brand
> knowledge that grounds the eventual reply.*

### Classify (`core/classify.py` + `llm.py`)

`classify()` sends the email to the **triage model (Haiku)** and parses strict
JSON into a `Classification` (category, priority, sentiment, language,
`needs_human`, `confidence`).

The actual Anthropic call lives in `llm.py` — the **only** file that imports the
`anthropic` SDK. The core depends on a small `LLMClient` that's injected, so the
vendor stays at the edge (same discipline as the ports).

**The important API pattern — `complete_json()` with assistant prefill:**

```python
# llm.py
resp = self._client.messages.create(
    model=model,                      # e.g. "claude-haiku-4-5-..."
    max_tokens=1024,
    temperature=0.0,                  # deterministic for triage
    system=system,                    # the schema + rules
    messages=[
        {"role": "user", "content": user},
        {"role": "assistant", "content": "{"},   # ← prefill forces JSON
    ],
)
text = "{" + "".join(b.text for b in resp.content if b.type == "text")
return json.loads(_trim_to_json(text))
```

Two tricks here every Anthropic developer should recognise:

* **Assistant prefill.** Seeding the assistant turn with `{` makes the model
  continue a JSON object instead of writing prose like "Sure! Here's the JSON:".
  You then re-prepend the `{` you primed it with.
* **`_trim_to_json()`** trims to the outermost `{...}` so any trailing chatter is
  ignored, then `json.loads`. If it's still not valid JSON, it raises loudly.

**Defensive coercion (`_coerce`).** Never trust raw model output. Invalid enum
values fall back to sensible defaults (`Category.OTHER`, `confidence` clamped to
`[0,1]`). And a **hard invariant is enforced in code, not left to the model**:
`billing` / `legal` / `refund` *always* get `needs_human=True`, no matter what
the LLM said. Prompts guide; code guarantees.

### Enrich (`core/enrich.py`)

Pure core that talks only to `CRMPort` and `KnowledgePort`:

1. `crm.find_contact(true_sender)` — look up the customer; create them if new
   (`upsert_contact`). See the adapter in `adapters/crm/internal_db.py`: plain
   parameterized SQL, `COALESCE(?, name)` to avoid clobbering existing fields on
   update.
2. `crm.history(contact_id)` — recent interactions, most-recent-first.
3. `knowledge.retrieve(brand, query, k=5)` — top-k brand-scoped KB chunks.

It returns an `Enrichment` dataclass bundling contact + history + KB chunks,
ready for drafting.

---

## Phase 4 — Draft: a grounded, brand-voiced reply

> *What's here: the strong-model drafting step and the local vector KB that keeps
> it from hallucinating.*

### Draft (`core/draft.py`)

`draft_reply()` calls the **draft model (Sonnet)** with a system prompt that
bakes in the brand voice and hard rules ("reply ONLY using the provided
KNOWLEDGE BASE and HISTORY — if the KB doesn't cover it, say you'll follow up";
"never promise refunds/legal/billing"). It formats the retrieved KB chunks and
contact history into the user prompt, gets back `{body_text, confidence}` via the
same `complete_json` prefill pattern, and returns a `Draft` that **also carries
the KB chunks it was given** (`used_kb_chunks`) — useful for showing a reviewer
*why* the agent said what it said.

This is **RAG (retrieval-augmented generation)**: retrieve relevant facts, stuff
them into the prompt, instruct the model to stay grounded in them.

### Knowledge base (`adapters/knowledge/chroma_kb.py`)

`ChromaKnowledge` implements `KnowledgePort` over a **local, persistent Chroma**
vector store — one collection per brand, so retrieval is naturally
brand-scoped:

```python
self.client = chromadb.PersistentClient(path=persist_path)
col = self.client.get_or_create_collection(name=f"kb_{brand}")
col.query(query_texts=[query], n_results=k)     # retrieval
col.upsert(documents=texts, metadatas=[...], ids=ids)   # ingestion
```

It uses Chroma's **default local embedding function** (ONNX MiniLM) so v1 needs
**no external embedding API** — it embeds on your machine. Distances are mapped
to a 0–1 score via `_dist_to_score()` (`1 / (1 + distance)`).

Documents get loaded by `scripts/ingest_kb.py`, which reads `.md`/`.txt` files
under a brand's `kb_path`, **chunks** them (1200 chars, 150 overlap), and upserts
with stable SHA-1 ids so re-ingesting is idempotent.

---

## Phase 5 — Orchestrate, route & approve

> *What's here: the LangGraph state machine that ties Phases 1–4 together, the
> autonomy decision, the durable human-approval gate, and the audit trail.*

This is the heart of the system. Three files: `core/pipeline.py` (orchestration),
`core/route.py` (the decision), `adapters/notification/web_queue.py` (the queue).

### The graph (`core/pipeline.py`)

`AgentPipeline._build_graph()` builds a **LangGraph `StateGraph`**. Each pipeline
stage is a node; a shared `PipelineState` (a `TypedDict`) flows between them and
each node returns a dict that gets merged in:

```python
g.add_node("parse", self._parse)
g.add_node("classify", self._classify)
...
g.set_entry_point("parse")
g.add_conditional_edges("prefilter", self._after_prefilter,
                        {"drop": "drop", "continue": "classify"})
g.add_conditional_edges("route", self._after_route,
                        {"auto_send": "auto_send",
                         "approve": "enqueue_approval",
                         "escalate": "escalate"})
graph = g.compile()
```

* **Plain edges** (`classify → enrich → draft → route`) are the linear spine.
* **Conditional edges** are the branches: a router function inspects the state
  and returns a key that selects the next node. That's how "junk → drop", and
  "route → send vs approve vs escalate" are expressed.
* `process(raw)` just calls `self.graph.invoke({"raw": raw})` and returns the
  final state.

Read the nodes in order (`_parse`, `_prefilter`, …) and you'll see each one is a
thin wrapper that calls a pure core function from Phases 1–4 and stashes the
result in state.

### The routing decision (`core/route.py`)

`decide_route()` is a **pure decision table** — no I/O, trivially testable
(`tests/test_route.py` covers every branch). Precedence matters:

1. **Always escalate** on negative sentiment, a `human_required` category
   (billing/legal/refund), confidence below threshold, or the `needs_human` flag.
2. **`draft_only` / `approval` modes never auto-send** → everything queues.
3. **`auto` mode** auto-sends *only* if the category is on the allowlist **and**
   confidence ≥ threshold.
4. Otherwise → approval.

Because the pipeline passes in `effective_mode()` (which already folds in the
kill switch), flipping `KILL_SWITCH` instantly forces everything back to
draft-only. **Ships in `draft_only`**: drafts everything, sends nothing, until a
human graduates it.

### The approval gate — a *durable pause*, not an in-memory interrupt

This is the single most important design decision, so it's worth dwelling on.

A human might take **hours** to approve a draft. You don't want a process (or a
LangGraph in-memory interrupt) held open that long. So instead of pausing
in-memory, the graph **writes the pending draft to the database and stops**:

* `_enqueue_approval` builds a `PendingApproval` and calls
  `notifier.enqueue_for_approval(...)`, which inserts a row into the `approvals`
  table (status `pending`). Then the message is moved to `Awaiting-Approval/`.
  The graph run **ends.**
* Resumption is a *completely separate entry point*: `process_approval()`. A
  human runs `run_agent.py approve <id>` (or edit/discard/escalate) hours later;
  that loads the saved approval and applies the decision — sending as the brand
  identity and moving the source to `Replied/`.

The payoff: the v1 loop stays **synchronous and crash-safe**. If the process dies
while a draft is awaiting approval, nothing is lost — the state is a DB row.

### Persistence & idempotency

* `_enrich` records the `messages` row **before** anything references it, so the
  foreign key from `approvals` is valid.
* **Idempotency** is enforced two ways: `messages.message_id` is `UNIQUE`, and
  `_parse` checks `already_processed(message_id)` up front — a duplicate is
  dropped immediately. INBOX membership is the primary signal (a handled message
  has been *moved out* of INBOX, so it's never fetched again); the
  `message_id` row and the `\Seen` flag are belt-and-suspenders backups.
* **Every** send / move / escalate / no-action writes an `audit_log` row via
  `AuditLog.write(actor, action, message_id, detail)`. That's the compliance
  trail.

### The approval queue adapter (`adapters/notification/web_queue.py`)

Implements `NotificationPort` over the `approvals` table. One pattern worth
seeing: it stores the structured fields it needs to reconstruct a
`PendingApproval` (brand, to-addr, subject, the classification) as JSON under a
`_meta` key in `context_json`, then rehydrates them with
`Classification.model_validate(...)` on read. A thin CLI surfaces the queue
today; a web view can replace it later without touching the port.

---

## Phase 6 — Lifecycle & maintenance (seamed, not built)

> *What's here: the cross-store operations that aren't part of per-message
> handling. Mostly deliberate stubs in v1.*

**File:** `maintenance.py`

These are **seams** — entry points that exist so the call sites are stable, but
whose dangerous fan-out is intentionally not wired yet:

* `offload_attachment()` — **implemented**: writes a blob to the local object
  store and returns a `storage_ref`. The contract is shaped so an S3 adapter can
  replace it later.
* `expunge_trash()` — **stub**: hard-deletes `Trash/` items past the retention
  grace period. No-op until Phase 6 proper.
* `warn_on_quota()` — **implemented**: logs a warning if mailbox usage exceeds a
  threshold (a full hub mailbox *bounces forwards*, a real operational risk).
* `purge_contact()` — **design-only seam**: GDPR erasure must fan out to SQLite,
  Chroma, *and* the attachment store. Deletion across stores is one-way and must
  not ship half-built, so this **logs and deletes nothing**. The intended
  fan-out is sketched in comments.

The lesson: a one-way destructive operation is left as an explicit, inert seam
rather than a half-finished feature.

---

## Where to go next

* **Run the tests** — `pytest -q`. Start with `tests/test_route.py` (pure, easy)
  then `tests/test_parse.py` (the `true_sender` rules) and finally
  `tests/test_pipeline.py` (the whole flow with stub adapters). The stubs are a
  great example of how ports make this testable.
* **Trace one message** — open `core/pipeline.py` and read the nodes top to
  bottom in graph order. Every node is a few lines that delegate to a Phase 1–4
  function.
* **Follow a config value** — pick `confidence_threshold` and trace it from
  `example.yaml` → `config.py` → `route.py`. That shows how config reaches a
  decision.
* **Add an adapter (the real exercise)** — implement a new `TransportPort` (say,
  a fake that reads `.eml` files from a folder) and wire it in `run_agent.py`'s
  `_build_transport`. If the core needs *zero* changes, you've understood the
  architecture.