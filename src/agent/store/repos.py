"""Repository helpers for messages, threads, and the audit log (§11).

These are not a port — they are internal persistence used by the pipeline and
finalize stage. The CRMPort (contacts/interactions) and NotificationPort
(approvals) have their own adapters; this covers the remaining tables.
"""

from __future__ import annotations

import json
import logging

from agent.core.models import Classification, Email
from agent.store.db import Database, new_id, now_iso

log = logging.getLogger(__name__)


class MessageRepo:
    """messages + threads: idempotency and thread tracking."""

    def __init__(self, db: Database):
        self.db = db

    def already_processed(self, message_id: str) -> bool:
        """Idempotency check (§13.3): has this Message-ID been recorded?"""
        return self.db.query_one(
            "SELECT 1 FROM messages WHERE message_id = ?", (message_id,)
        ) is not None

    def ensure_thread(self, email: Email, contact_id: str | None) -> str:
        """Find or create a thread by references/subject; returns thread_id."""
        # Prefer threading by the root reference if present.
        root = email.references[0] if email.references else (email.in_reply_to or None)
        if root:
            row = self.db.query_one(
                "SELECT id FROM threads WHERE subject = ? AND brand IS ?",
                (_normalize_subject(email.subject), email.brand),
            )
            if row:
                return row["id"]
        tid = new_id()
        ts = now_iso()
        self.db.execute(
            "INSERT INTO threads (id, brand, contact_id, status, owner, subject, sla_due, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, email.brand, contact_id, "open", None,
             _normalize_subject(email.subject), None, ts, ts),
        )
        return tid

    def record_message(
        self,
        email: Email,
        classification: Classification | None,
        thread_id: str | None,
        folder: str,
    ) -> str:
        """Insert (or no-op on conflict) a messages row. Returns the row id."""
        mid = new_id()
        self.db.execute(
            "INSERT OR IGNORE INTO messages "
            "(id, uid, message_id, thread_id, brand, category, confidence, processed_at, folder) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                mid,
                email.uid,
                email.message_id,
                thread_id,
                email.brand,
                classification.category.value if classification else None,
                classification.confidence if classification else None,
                now_iso(),
                folder,
            ),
        )
        return mid

    def set_folder(self, message_id: str, folder: str) -> None:
        self.db.execute(
            "UPDATE messages SET folder = ? WHERE message_id = ?", (folder, message_id)
        )

    def record_attachments(self, message_id: str, attachments) -> None:
        """Persist attachment references for a message (§10 — refs, not blobs).

        Idempotent per message: clears any prior rows for this message_id first,
        so a re-run records the current set without duplicating.
        """
        if not attachments:
            return
        self.db.execute("DELETE FROM attachments WHERE message_id = ?", (message_id,))
        for att in attachments:
            self.db.execute(
                "INSERT INTO attachments "
                "(id, message_id, filename, content_type, size, storage_ref, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (new_id(), message_id, att.filename, att.content_type,
                 att.size, att.storage_ref, now_iso()),
            )


class AuditLog:
    """audit_log: every send/move/escalate writes a row (§13.6)."""

    def __init__(self, db: Database):
        self.db = db

    def write(self, actor: str, action: str, message_id: str | None, detail: dict | None = None) -> None:
        self.db.execute(
            "INSERT INTO audit_log (id, ts, actor, action, message_id, detail_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (new_id(), now_iso(), actor, action, message_id, json.dumps(detail or {})),
        )
        log.info("audit: actor=%s action=%s message_id=%s", actor, action, message_id)


def _normalize_subject(subject: str) -> str:
    """Strip Re:/Fwd: prefixes for thread grouping."""
    import re

    return re.sub(r"^\s*(re|fwd?|aw|sv)\s*:\s*", "", subject or "", flags=re.IGNORECASE).strip()