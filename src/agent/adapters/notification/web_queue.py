"""Web/CLI approval queue — NotificationPort adapter (§3, §7.7).

v1 surface for human-in-the-loop approvals, backed by the approvals table.
A pending approval persists until a human resolves it (send|edit|discard|
escalate). A thin CLI lives in scripts/run_agent.py; a minimal web view can be
added later without touching the port.
"""

from __future__ import annotations

import json
import logging

from agent.core.models import (
    ApprovalDecision,
    Classification,
    DecisionType,
    Draft,
    PendingApproval,
)
from agent.store.db import Database, new_id, now_iso

log = logging.getLogger(__name__)


class WebQueueNotifier:
    """NotificationPort over the approvals table."""

    def __init__(self, db: Database):
        self.db = db

    def enqueue_for_approval(self, draft: PendingApproval) -> str:
        approval_id = draft.id or new_id()
        context = dict(draft.context or {})
        # Fold the structured fields we need to reconstruct into context_json.
        context["_meta"] = {
            "brand": draft.brand,
            "to_addr": draft.to_addr,
            "subject": draft.subject,
            "confidence": draft.draft.confidence,
            "classification": draft.classification.model_dump(mode="json")
            if draft.classification
            else None,
        }
        self.db.execute(
            "INSERT INTO approvals (id, message_id, draft_body, context_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                approval_id,
                draft.message_id,
                draft.draft.body_text,
                json.dumps(context),
                "pending",
                now_iso(),
            ),
        )
        log.info("enqueued approval %s for message %s", approval_id, draft.message_id)
        return approval_id

    def list_pending(self) -> list[PendingApproval]:
        rows = self.db.query_all(
            "SELECT * FROM approvals WHERE status = 'pending' ORDER BY created_at ASC"
        )
        return [_row_to_pending(r) for r in rows]

    def get(self, approval_id: str) -> PendingApproval | None:
        row = self.db.query_one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        return _row_to_pending(row) if row else None

    def resolve(self, approval_id: str, decision: ApprovalDecision) -> None:
        self.db.execute(
            "UPDATE approvals SET status = ?, resolved_at = ?, "
            "draft_body = CASE WHEN ? IS NOT NULL THEN ? ELSE draft_body END "
            "WHERE id = ?",
            (
                decision.decision.value,
                now_iso(),
                decision.edited_body,
                decision.edited_body,
                approval_id,
            ),
        )
        log.info("resolved approval %s -> %s", approval_id, decision.decision.value)


def _row_to_pending(row) -> PendingApproval:
    context = json.loads(row["context_json"]) if row["context_json"] else {}
    meta = context.get("_meta", {}) or {}
    classification = None
    if meta.get("classification"):
        classification = Classification.model_validate(meta["classification"])
    return PendingApproval(
        id=row["id"],
        message_id=row["message_id"],
        brand=meta.get("brand"),
        to_addr=meta.get("to_addr", ""),
        subject=meta.get("subject", ""),
        draft=Draft(body_text=row["draft_body"] or "", confidence=meta.get("confidence", 0.0)),
        classification=classification,
        context={k: v for k, v in context.items() if k != "_meta"},
        created_at=row["created_at"],
    )