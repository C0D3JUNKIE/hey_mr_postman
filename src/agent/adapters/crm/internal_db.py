"""Internal CRM adapter — contacts + interactions over the SQLite store (§3, §11).

Implements CRMPort with the project's own tables. The external-CRM seam
(HubSpot/Salesforce) is left for later; create_lead is a logged stub for v1.
"""

from __future__ import annotations

import json
import logging

from agent.core.models import Contact, Direction, Interaction
from agent.store.db import Database, new_id, now_iso

log = logging.getLogger(__name__)


class InternalDbCRM:
    """CRMPort backed by the internal SQLite tables."""

    def __init__(self, db: Database):
        self.db = db

    # ── contacts ──

    def find_contact(self, email: str) -> Contact | None:
        row = self.db.query_one(
            "SELECT * FROM contacts WHERE email = ?", (email.strip().lower(),)
        )
        return _row_to_contact(row) if row else None

    def upsert_contact(self, contact: Contact) -> Contact:
        email = contact.email.strip().lower()
        existing = self.db.query_one("SELECT * FROM contacts WHERE email = ?", (email,))
        ts = now_iso()
        tags_json = json.dumps(contact.tags or [])
        if existing:
            self.db.execute(
                "UPDATE contacts SET name = COALESCE(?, name), "
                "brand = COALESCE(?, brand), last_seen = ?, tags = ? WHERE email = ?",
                (contact.name, contact.brand, ts, tags_json, email),
            )
            row = self.db.query_one("SELECT * FROM contacts WHERE email = ?", (email,))
            return _row_to_contact(row)

        cid = contact.id or new_id()
        self.db.execute(
            "INSERT INTO contacts (id, email, name, brand, first_seen, last_seen, tags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cid, email, contact.name, contact.brand, ts, ts, tags_json),
        )
        row = self.db.query_one("SELECT * FROM contacts WHERE id = ?", (cid,))
        return _row_to_contact(row)

    # ── interactions ──

    def log_interaction(self, contact_id: str, event: Interaction) -> None:
        self.db.execute(
            "INSERT INTO interactions (id, contact_id, thread_id, direction, channel, summary, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event.id or new_id(),
                contact_id,
                event.thread_id,
                event.direction.value if isinstance(event.direction, Direction) else event.direction,
                event.channel,
                event.summary,
                event.ts.isoformat() if event.ts else now_iso(),
            ),
        )

    def history(self, contact_id: str, limit: int = 10) -> list[Interaction]:
        rows = self.db.query_all(
            "SELECT * FROM interactions WHERE contact_id = ? ORDER BY ts DESC LIMIT ?",
            (contact_id, limit),
        )
        return [_row_to_interaction(r) for r in rows]

    # ── lead creation (stub for v1, §5) ──

    def create_lead(self, contact_id: str, details: dict) -> None:
        log.info("create_lead stub — contact=%s details=%s", contact_id, json.dumps(details))


def _row_to_contact(row) -> Contact:
    return Contact(
        id=row["id"],
        email=row["email"],
        name=row["name"],
        brand=row["brand"],
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        tags=json.loads(row["tags"]) if row["tags"] else [],
    )


def _row_to_interaction(row) -> Interaction:
    return Interaction(
        id=row["id"],
        contact_id=row["contact_id"],
        thread_id=row["thread_id"],
        direction=Direction(row["direction"]) if row["direction"] else Direction.INBOUND,
        channel=row["channel"] or "email",
        summary=row["summary"] or "",
        ts=row["ts"],
    )