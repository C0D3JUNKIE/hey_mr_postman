-- Hey Mr. Postman email agent — state store (§11)
-- SQLite for v1, written Postgres-compatible (TEXT timestamps, no SQLite-only types)
-- so migration is trivial. message_id UNIQUE + INBOX-membership give
-- belt-and-suspenders idempotency.

CREATE TABLE IF NOT EXISTS contacts (
    id          TEXT PRIMARY KEY,
    email       TEXT UNIQUE NOT NULL,
    name        TEXT,
    brand       TEXT,
    first_seen  TEXT,
    last_seen   TEXT,
    tags        TEXT            -- JSON array
);

CREATE TABLE IF NOT EXISTS threads (
    id          TEXT PRIMARY KEY,
    brand       TEXT,
    contact_id  TEXT REFERENCES contacts(id),
    status      TEXT,
    owner       TEXT,
    subject     TEXT,
    sla_due     TEXT,
    created_at  TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS interactions (
    id          TEXT PRIMARY KEY,
    contact_id  TEXT REFERENCES contacts(id),
    thread_id   TEXT REFERENCES threads(id),
    direction   TEXT,           -- inbound | outbound
    channel     TEXT,           -- email
    summary     TEXT,
    ts          TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    uid         TEXT,
    message_id  TEXT UNIQUE NOT NULL,
    thread_id   TEXT REFERENCES threads(id),
    brand       TEXT,
    category    TEXT,
    confidence  REAL,
    processed_at TEXT,
    folder      TEXT
);

CREATE TABLE IF NOT EXISTS approvals (
    id           TEXT PRIMARY KEY,
    message_id   TEXT REFERENCES messages(message_id),
    draft_body   TEXT,
    context_json TEXT,
    status       TEXT,           -- pending | send | edit | discard | escalate
    created_at   TEXT,
    resolved_at  TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          TEXT PRIMARY KEY,
    ts          TEXT,
    actor       TEXT,
    action      TEXT,
    message_id  TEXT,
    detail_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_interactions_contact ON interactions(contact_id);
CREATE INDEX IF NOT EXISTS idx_messages_message_id  ON messages(message_id);
CREATE INDEX IF NOT EXISTS idx_approvals_status     ON approvals(status);
CREATE INDEX IF NOT EXISTS idx_threads_contact      ON threads(contact_id);