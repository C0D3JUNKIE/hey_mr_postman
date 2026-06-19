"""Lifecycle & maintenance seams (§10).

Houses the cross-store operations that are not part of the per-message pipeline:
attachment offload, trash retention/expunge, and the GDPR purge seam.

Per the spec, `purge_contact` is a SEAM in v1: the fan-out to all three stores
(DB, Chroma, attachment store) is sketched but intentionally not fully wired —
deletion across stores is a one-way operation and must not ship half-built.
"""

from __future__ import annotations

import logging
from pathlib import Path

from agent.config import ScenarioConfig
from agent.store.db import Database

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


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-._@" else "_" for c in name)[:120]