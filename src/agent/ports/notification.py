"""NotificationPort — where approvals surface.

v1 adapter: web/CLI queue. Future: Slack, email, push.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent.core.models import ApprovalDecision, PendingApproval


@runtime_checkable
class NotificationPort(Protocol):
    def enqueue_for_approval(self, draft: PendingApproval) -> str:
        """Queue a draft for human review; returns the approval id."""
        ...

    def list_pending(self) -> list[PendingApproval]:
        ...

    def resolve(self, approval_id: str, decision: ApprovalDecision) -> None:
        """Record a human decision against a pending approval."""
        ...