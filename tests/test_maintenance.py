"""SLA follow-up timer + daily digest (§7.10, §14 Phase 6)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent import maintenance
from agent.adapters.notification.web_queue import WebQueueNotifier
from agent.core.models import Draft, PendingApproval
from agent.store.db import Database, new_id, now_iso
from agent.store.repos import AuditLog


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "agent.db")


def _ensure_message(db: Database, message_id: str, thread_id: str | None = None) -> None:
    """approvals.message_id has an FK to messages — insert the row first."""
    db.execute(
        "INSERT OR IGNORE INTO messages (id, uid, message_id, thread_id, brand) "
        "VALUES (?, ?, ?, ?, ?)",
        (new_id(), "1", message_id, thread_id, "siteA"),
    )


def _enqueue(notifier: WebQueueNotifier, db: Database, message_id: str, to_addr: str, subject: str = "Help") -> str:
    _ensure_message(db, message_id)
    return notifier.enqueue_for_approval(
        PendingApproval(
            message_id=message_id,
            brand="siteA",
            to_addr=to_addr,
            subject=subject,
            draft=Draft(body_text="draft body", confidence=0.9),
        )
    )


def _backdate(db: Database, approval_id: str, hours: float) -> None:
    """Make an approval look like it was queued `hours` ago."""
    old = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    db.execute("UPDATE approvals SET created_at = ? WHERE id = ?", (old, approval_id))


def _link_thread(db: Database, message_id: str) -> str:
    """Insert a thread and link the message row to it (message added by _enqueue)."""
    tid = new_id()
    ts = now_iso()
    db.execute(
        "INSERT INTO threads (id, brand, status, subject, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tid, "siteA", "open", "Help", ts, ts),
    )
    db.execute("UPDATE messages SET thread_id = ? WHERE message_id = ?", (tid, message_id))
    return tid


# ─────────────────────────── SLA follow-up ───────────────────────────


def test_overdue_draft_breaches_sla(config, db):
    notifier = WebQueueNotifier(db)
    audit = AuditLog(db)
    aid = _enqueue(notifier, db, "<m1@x>", "cust@example.com")
    _backdate(db, aid, 48)  # default SLA is 24h

    breaches = maintenance.run_sla_followups(config, db, audit=audit)

    assert len(breaches) == 1
    assert breaches[0].message_id == "<m1@x>"
    assert breaches[0].hours_waiting >= 24
    # audit row recorded
    rows = db.query_all("SELECT 1 FROM audit_log WHERE action = 'sla_followup'")
    assert len(rows) == 1


def test_fresh_draft_is_not_overdue(config, db):
    notifier = WebQueueNotifier(db)
    _enqueue(notifier, db, "<m2@x>", "cust@example.com")  # created just now

    assert maintenance.run_sla_followups(config, db, audit=AuditLog(db)) == []


def test_sla_followup_is_idempotent(config, db):
    notifier = WebQueueNotifier(db)
    audit = AuditLog(db)
    aid = _enqueue(notifier, db, "<m3@x>", "cust@example.com")
    _backdate(db, aid, 48)

    first = maintenance.run_sla_followups(config, db, audit=audit)
    second = maintenance.run_sla_followups(config, db, audit=audit)

    assert len(first) == 1
    assert second == []  # already nudged → no re-alert
    assert len(db.query_all("SELECT 1 FROM audit_log WHERE action = 'sla_followup'")) == 1


def test_sla_followup_marks_thread_overdue(config, db):
    notifier = WebQueueNotifier(db)
    aid = _enqueue(notifier, db, "<m4@x>", "cust@example.com")
    tid = _link_thread(db, "<m4@x>")
    _backdate(db, aid, 48)

    maintenance.run_sla_followups(config, db, audit=AuditLog(db))

    row = db.query_one("SELECT status, sla_due FROM threads WHERE id = ?", (tid,))
    assert row["status"] == "overdue"
    assert row["sla_due"] is not None


def test_sla_disabled_is_noop(config, db):
    config.sla.enabled = False
    notifier = WebQueueNotifier(db)
    aid = _enqueue(notifier, db, "<m5@x>", "cust@example.com")
    _backdate(db, aid, 48)

    assert maintenance.run_sla_followups(config, db, audit=AuditLog(db)) == []


# ─────────────────────────── digest ───────────────────────────


def test_digest_summarizes_activity_and_queue(config, db):
    notifier = WebQueueNotifier(db)
    audit = AuditLog(db)
    audit.write("agent", "auto_send", "<a@x>", {})
    audit.write("human", "send", "<b@x>", {})
    audit.write("agent", "escalate", "<c@x>", {})

    overdue_id = _enqueue(notifier, db, "<d@x>", "cust@example.com")
    _backdate(db, overdue_id, 48)
    _enqueue(notifier, db, "<e@x>", "fresh@example.com")  # in-window, not overdue

    report = maintenance.build_digest(config, db)

    assert report.activity["auto-sent"] == 1
    assert report.activity["replied (approved)"] == 1
    assert report.activity["escalated (auto)"] == 1
    assert report.pending_total == 2
    assert len(report.overdue) == 1
    assert report.overdue[0].message_id == "<d@x>"

    text = maintenance.render_digest(report)
    assert "daily digest" in text
    assert "past SLA" in text


def test_digest_empty(config, db):
    report = maintenance.build_digest(config, db)
    assert report.activity == {}
    assert report.pending_total == 0
    text = maintenance.render_digest(report)
    assert "no activity in window" in text