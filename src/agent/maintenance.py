"""Lifecycle & maintenance seams (§10).

Houses the cross-store operations that are not part of the per-message pipeline:
attachment offload, trash retention/expunge, and the GDPR purge seam.

Per the spec, `purge_contact` is a SEAM in v1: the fan-out to all three stores
(DB, Chroma, attachment store) is sketched but intentionally not fully wired —
deletion across stores is a one-way operation and must not ship half-built.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.config import ScenarioConfig
from agent.store.db import Database, now_iso

log = logging.getLogger(__name__)


def offload_attachment(config: ScenarioConfig, message_id: str, filename: str, blob: bytes) -> str:
    """Write an attachment to the local object store; return its storage_ref (§10).

    v1 keeps a disk path reference, not the blob, in the message record. An S3
    adapter can replace this later behind the same return contract.
    """
    base = Path(config.storage.attachments_path) / _safe(message_id)
    base.mkdir(parents=True, exist_ok=True)
    dest = base / _safe(filename)
    dest.write_bytes(blob)
    ref = str(dest)
    log.info("offloaded attachment %s (%d bytes) → %s", filename, len(blob), ref)
    return ref


def expunge_trash(config: ScenarioConfig, transport) -> None:
    """Permanently remove Trash/ items past the retention grace period (§10).

    Soft delete moved them to Trash/ already; this is the hard delete. The IMAP
    side (selecting Trash, EXPUNGE of \\Deleted older than grace_days) lives in
    the transport adapter; this orchestrates it. Left minimal for v1.
    """
    grace = config.retention.trash_grace_days
    log.info("expunge_trash: grace=%d days (transport-driven; no-op stub in v1)", grace)
    # Intentionally a no-op stub: wire to transport.expunge_older_than(grace) in Phase 6.


def warn_on_quota(config: ScenarioConfig, transport, threshold: float = 0.9) -> bool:
    """Log a warning if mailbox usage exceeds `threshold` of quota (§10).

    A full mailbox bounces forwards, so this is a real operational risk. Returns
    True if a warning was emitted.
    """
    usage = getattr(transport, "mailbox_usage", lambda: None)()
    if not usage:
        return False
    used, quota = usage
    if quota and used / quota >= threshold:
        log.warning("mailbox usage at %.0f%% of quota (%d / %d bytes)", 100 * used / quota, used, quota)
        return True
    return False


def purge_contact(config: ScenarioConfig, db: Database, email: str) -> None:
    """GDPR erasure seam (§10, §16) — fan out to all three stores.

    DESIGN-ONLY in v1: this entry point exists so the call site is stable, but
    the destructive fan-out is deliberately NOT executed. Implement deliberately,
    behind an explicit confirmation, when erasure is actually needed.
    """
    log.warning(
        "purge_contact(%s) called — v1 SEAM ONLY, no data deleted. "
        "Fan-out targets: (1) SQLite contacts/interactions/threads/messages/approvals, "
        "(2) Chroma per-brand collections, (3) attachment object store.",
        email,
    )
    # Sketch of the intended fan-out (kept inert):
    #   contact = db.query_one("SELECT id FROM contacts WHERE email = ?", (email,))
    #   if contact: ... delete interactions, threads, messages, approvals, contact ...
    #   chroma_collection.delete(where={"contact_email": email})
    #   shutil.rmtree(attachments_path / contact_id)
    return None


# ─────────────────────────── SLA follow-up timer (§7.10) ───────────────────────────


@dataclass
class OverdueApproval:
    """A draft that has waited in the approval queue past the SLA window."""

    approval_id: str
    message_id: str
    brand: str | None
    to_addr: str
    subject: str
    created_at: str
    hours_waiting: float
    thread_id: str | None = None


def list_overdue_pending(
    config: ScenarioConfig, db: Database, now: datetime | None = None
) -> list[OverdueApproval]:
    """All pending approvals older than the SLA window, oldest first (read-only).

    This is the query the digest and the sweep share. It performs no writes, so
    it can be called freely (e.g. to render the digest) without side effects.
    """
    now = now or _utcnow()
    cutoff = now - timedelta(hours=config.sla.follow_up_hours)
    rows = db.query_all(
        "SELECT a.id, a.message_id, a.context_json, a.created_at, m.thread_id, m.brand "
        "FROM approvals a LEFT JOIN messages m ON m.message_id = a.message_id "
        "WHERE a.status = 'pending' ORDER BY a.created_at ASC"
    )
    overdue: list[OverdueApproval] = []
    for r in rows:
        created = _parse_iso(r["created_at"])
        if created is None or created > cutoff:
            continue
        meta = _approval_meta(r["context_json"])
        overdue.append(
            OverdueApproval(
                approval_id=r["id"],
                message_id=r["message_id"],
                brand=r["brand"] or meta.get("brand"),
                to_addr=meta.get("to_addr", ""),
                subject=meta.get("subject", ""),
                created_at=r["created_at"],
                hours_waiting=round((now - created).total_seconds() / 3600.0, 1),
                thread_id=r["thread_id"],
            )
        )
    return overdue


def run_sla_followups(
    config: ScenarioConfig,
    db: Database,
    audit=None,
    now: datetime | None = None,
) -> list[OverdueApproval]:
    """Nudge drafts that breached SLA — once each (§7.10).

    Intended to run on the poll loop as v1's "background" timer. For each newly
    overdue approval (no prior ``sla_followup`` audit row) it: flags the thread
    ``overdue`` with its ``sla_due``, and writes an ``sla_followup`` audit row.
    The visible nudge is the daily digest, which highlights these. Returns the
    items newly flagged this sweep (already-nudged ones are skipped, so repeated
    sweeps don't re-alert).
    """
    if not config.sla.enabled:
        return []
    now = now or _utcnow()
    newly: list[OverdueApproval] = []
    for item in list_overdue_pending(config, db, now=now):
        already = db.query_one(
            "SELECT 1 FROM audit_log WHERE action = 'sla_followup' AND message_id = ?",
            (item.message_id,),
        )
        if already:
            continue
        if item.thread_id:
            created = _parse_iso(item.created_at)
            due = (created + timedelta(hours=config.sla.follow_up_hours)).isoformat() if created else None
            db.execute(
                "UPDATE threads SET status = 'overdue', sla_due = ?, updated_at = ? WHERE id = ?",
                (due, now_iso(), item.thread_id),
            )
        if audit is not None:
            audit.write(
                "agent",
                "sla_followup",
                item.message_id,
                {
                    "approval_id": item.approval_id,
                    "hours_waiting": item.hours_waiting,
                    "sla_hours": config.sla.follow_up_hours,
                },
            )
        log.warning(
            "SLA breach: approval %s for %s waiting %.1fh (> %.0fh)",
            item.approval_id,
            item.to_addr or item.message_id,
            item.hours_waiting,
            config.sla.follow_up_hours,
        )
        newly.append(item)
    return newly


# ─────────────────────────── daily digest (§14 Phase 6) ───────────────────────────

# audit_log action → human-readable headline bucket for the digest.
_DIGEST_BUCKETS: dict[str, str] = {
    "auto_send": "auto-sent",
    "send": "replied (approved)",
    "send_edited": "replied (edited)",
    "enqueue_approval": "queued for approval",
    "escalate": "escalated (auto)",
    "escalate_manual": "escalated (manual)",
    "discard": "discarded",
    "no_action": "no action",
    "sla_followup": "SLA breaches",
}


@dataclass
class DigestReport:
    window_hours: float
    generated_at: str
    activity: dict[str, int] = field(default_factory=dict)  # bucket label → count
    pending_total: int = 0
    overdue: list[OverdueApproval] = field(default_factory=list)


def build_digest(
    config: ScenarioConfig,
    db: Database,
    now: datetime | None = None,
    window_hours: float | None = None,
) -> DigestReport:
    """Summarize agent activity over the lookback window plus the live queue state."""
    now = now or _utcnow()
    window = window_hours if window_hours is not None else config.digest.window_hours
    since = (now - timedelta(hours=window)).isoformat()

    rows = db.query_all(
        "SELECT action, COUNT(*) AS n FROM audit_log WHERE ts >= ? GROUP BY action",
        (since,),
    )
    activity: dict[str, int] = {}
    for r in rows:
        label = _DIGEST_BUCKETS.get(r["action"])
        if label:
            activity[label] = activity.get(label, 0) + r["n"]

    pending = db.query_one("SELECT COUNT(*) AS n FROM approvals WHERE status = 'pending'")
    return DigestReport(
        window_hours=window,
        generated_at=now.isoformat(),
        activity=activity,
        pending_total=pending["n"] if pending else 0,
        overdue=list_overdue_pending(config, db, now=now),
    )


def render_digest(report: DigestReport) -> str:
    """Render a DigestReport as plain text (for CLI/log; an email/Slack seam later)."""
    lines = [
        f"Hey Mr. Postman — daily digest (last {report.window_hours:g}h)",
        f"generated: {report.generated_at}",
        "",
        "Activity:",
    ]
    if report.activity:
        for label in sorted(report.activity):
            lines.append(f"  {report.activity[label]:>4}  {label}")
    else:
        lines.append("  (no activity in window)")

    lines += ["", f"Approval queue: {report.pending_total} pending"]
    if report.overdue:
        lines.append(f"  ⚠ {len(report.overdue)} past SLA:")
        for o in report.overdue:
            subj = (o.subject or "(no subject)")[:50]
            lines.append(f"    - {o.hours_waiting:>5.1f}h  {o.to_addr or o.message_id}  “{subj}”")
    else:
        lines.append("  none past SLA")
    return "\n".join(lines)


# ─────────────────────────── helpers ───────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse a stored ISO-8601 timestamp; assume UTC if it carries no offset."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _approval_meta(context_json: str | None) -> dict:
    """Pull the `_meta` block the notifier folds into context_json (brand/to/subject)."""
    import json

    if not context_json:
        return {}
    try:
        return (json.loads(context_json) or {}).get("_meta", {}) or {}
    except (ValueError, TypeError):
        return {}


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-._@" else "_" for c in name)[:120]