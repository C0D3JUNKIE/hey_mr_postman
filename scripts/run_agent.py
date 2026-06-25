"""Composition root + main loop + approval CLI.

Wires config → adapters → core (the only place concrete adapters are chosen),
then runs the ingest loop or services the approval queue.

Usage:
    python -m scripts.run_agent run            # poll loop (default)
    python -m scripts.run_agent once           # single ingest pass, then exit
    python -m scripts.run_agent approvals       # list pending approvals
    python -m scripts.run_agent approve <id>    # send a queued draft
    python -m scripts.run_agent edit <id> "new body"
    python -m scripts.run_agent discard <id>
    python -m scripts.run_agent escalate <id>
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Allow `python scripts/run_agent.py` as well as `-m scripts.run_agent`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent.adapters.crm.internal_db import InternalDbCRM
from agent.adapters.knowledge.chroma_kb import ChromaKnowledge
from agent.adapters.notification.web_queue import WebQueueNotifier
from agent.adapters.transport.imap_smtp import ImapSmtpTransport
from agent.config import ScenarioConfig, load_scenario
from agent.core.models import ApprovalDecision, DecisionType
from agent.core.pipeline import AgentPipeline
from agent.llm import LLMClient
from agent import maintenance
from agent.store.db import Database
from agent.store.repos import AuditLog, MessageRepo

log = logging.getLogger("mailagent")

DEFAULT_SCENARIO = "config/scenarios/example.yaml"


class App:
    """Holds the composed object graph."""

    def __init__(self, config: ScenarioConfig):
        self.config = config
        self.db = Database(config.storage.db_path)
        # Adapters (one per port, selected by config.transport.type).
        self.transport = _build_transport(config)
        self.crm = InternalDbCRM(self.db)
        self.knowledge = ChromaKnowledge(config.storage.chroma_path)
        self.notifier = WebQueueNotifier(self.db)
        self.llm = LLMClient()
        self.messages = MessageRepo(self.db)
        self.audit = AuditLog(self.db)
        self.pipeline = AgentPipeline(
            config=config,
            transport=self.transport,
            crm=self.crm,
            knowledge=self.knowledge,
            notifier=self.notifier,
            llm=self.llm,
            message_repo=self.messages,
            audit=self.audit,
        )

    # ── ingest ──

    def run_once(self) -> int:
        raws = self.transport.read_new()
        log.info("ingest: %d new message(s)", len(raws))
        for raw in raws:
            try:
                state = self.pipeline.process(raw)
                log.info(
                    "processed uid=%s outcome=%s reason=%s",
                    raw.uid,
                    state.get("outcome"),
                    state.get("reason", ""),
                )
            except Exception:
                log.exception("pipeline failed for uid=%s; leaving in INBOX for retry", raw.uid)
        return len(raws)

    def run_loop(self) -> None:
        interval = self.config.transport.imap.poll_interval_seconds
        mode = self.config.effective_mode()
        log.info("starting poll loop (interval=%ss, mode=%s)", interval, mode)
        if self.config.kill_switch_engaged():
            log.warning("KILL_SWITCH engaged → forcing draft_only")
        while True:
            try:
                self.run_once()
                self.run_sla_followups()
            except Exception:
                log.exception("ingest pass failed; will retry")
            time.sleep(interval)

    # ── follow-up / digest (Phase 6) ──

    def run_sla_followups(self) -> int:
        """v1 'background' SLA timer: nudge drafts that have waited too long."""
        breaches = maintenance.run_sla_followups(
            self.config, self.db, audit=self.audit
        )
        if breaches:
            log.warning("%d approval(s) breached SLA this sweep", len(breaches))
        return len(breaches)

    def print_digest(self) -> None:
        report = maintenance.build_digest(self.config, self.db)
        print(maintenance.render_digest(report))

    # ── approvals ──

    def list_pending(self) -> None:
        pending = self.notifier.list_pending()
        if not pending:
            print("No pending approvals.")
            return
        for p in pending:
            cat = p.classification.category.value if p.classification else "?"
            conf = p.draft.confidence
            print(f"\n── approval {p.id}  [{cat} conf={conf:.2f}]  brand={p.brand}")
            print(f"   to:      {p.to_addr}")
            print(f"   subject: {p.subject}")
            print(f"   reason:  {p.context.get('route_reason', '')}")
            print(f"   draft:\n{_indent(p.draft.body_text)}")

    def resolve(self, approval_id: str, decision: ApprovalDecision) -> None:
        approval = self.notifier.get(approval_id)
        if approval is None:
            print(f"No such approval: {approval_id}", file=sys.stderr)
            sys.exit(1)
        outcome = self.pipeline.process_approval(approval, decision)
        print(f"approval {approval_id} → {outcome}")


def _build_transport(config: ScenarioConfig):
    if config.transport.type == "imap_smtp":
        return ImapSmtpTransport(config)
    raise ValueError(f"unsupported transport type: {config.transport.type!r}")


def _indent(text: str, prefix: str = "      ") -> str:
    return "\n".join(prefix + line for line in (text or "").splitlines())


def _setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    _setup_logging()

    parser = argparse.ArgumentParser(description="Hey Mr. Postman email agent")
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO, help="scenario YAML path")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("run", help="poll loop (default)")
    sub.add_parser("once", help="single ingest pass")
    sub.add_parser("approvals", help="list pending approvals")
    sub.add_parser("sla", help="run SLA follow-up sweep (nudge overdue drafts)")
    sub.add_parser("digest", help="print the activity digest")
    for name in ("approve", "discard", "escalate"):
        p = sub.add_parser(name)
        p.add_argument("approval_id")
    p_edit = sub.add_parser("edit")
    p_edit.add_argument("approval_id")
    p_edit.add_argument("body")

    args = parser.parse_args(argv)
    config = load_scenario(args.scenario)
    app = App(config)

    cmd = args.cmd or "run"
    if cmd == "run":
        app.run_loop()
    elif cmd == "once":
        app.run_once()
    elif cmd == "approvals":
        app.list_pending()
    elif cmd == "sla":
        n = app.run_sla_followups()
        print(f"{n} approval(s) newly breached SLA")
    elif cmd == "digest":
        app.print_digest()
    elif cmd == "approve":
        app.resolve(args.approval_id, ApprovalDecision(decision=DecisionType.SEND))
    elif cmd == "edit":
        app.resolve(
            args.approval_id,
            ApprovalDecision(decision=DecisionType.EDIT, edited_body=args.body),
        )
    elif cmd == "discard":
        app.resolve(args.approval_id, ApprovalDecision(decision=DecisionType.DISCARD))
    elif cmd == "escalate":
        app.resolve(args.approval_id, ApprovalDecision(decision=DecisionType.ESCALATE))


if __name__ == "__main__":
    main()