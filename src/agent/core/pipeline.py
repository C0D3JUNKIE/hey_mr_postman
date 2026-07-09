"""Processing pipeline — the LangGraph orchestration (§7).

The graph processes ONE inbound message end-to-end. Ingest (pulling many
messages) is the outer loop in run_agent.py; it invokes this graph per message.

Stages → nodes:
    parse → prefilter → classify → enrich → draft → route
                                                      ├─ auto_send ─┐
                                                      ├─ approve ───┤→ finalize
                                                      └─ escalate ──┘

The approval gate is realized as a DURABLE pause: when a human decision is
required we enqueue to the NotificationPort and stop. Resumption happens out of
band (process_approval), because a human may take hours — far longer than any
in-memory interrupt should hold a process open. This keeps the v1 loop
synchronous and crash-safe while preserving the interrupt semantics of §7.7.

core/pipeline.py imports only ports, models, sibling core stages, config, and
langgraph (the orchestration framework). Never imaplib/smtplib/chromadb.
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from agent.config import ScenarioConfig
from agent.core import classify as classify_stage
from agent.core import draft as draft_stage
from agent.core import enrich as enrich_stage
from agent.core import route as route_stage
from agent.core.models import (
    Direction,
    Email,
    Interaction,
    OutgoingEmail,
    PendingApproval,
    RawMessage,
    RouteAction,
)
from agent.core.prefilter import prefilter
from agent.ports.crm import CRMPort
from agent.ports.knowledge import KnowledgePort
from agent.ports.notification import NotificationPort
from agent.ports.transport import TransportPort

log = logging.getLogger(__name__)


class PipelineState(TypedDict, total=False):
    raw: RawMessage
    headers: dict[str, str]
    email: Email
    classification: Any
    enrichment: Any
    draft: Any
    route: Any
    outcome: str          # replied | awaiting | escalated | no_action | auto_sent
    folder: str
    reason: str
    approval_id: str
    thread_id: str


class AgentPipeline:
    """Wires the core stages and ports into a runnable LangGraph."""

    def __init__(
        self,
        *,
        config: ScenarioConfig,
        transport: TransportPort,
        crm: CRMPort,
        knowledge: KnowledgePort,
        notifier: NotificationPort,
        llm,
        message_repo,
        audit,
    ):
        self.config = config
        self.transport = transport
        self.crm = crm
        self.knowledge = knowledge
        self.notifier = notifier
        self.llm = llm
        self.messages = message_repo
        self.audit = audit
        self.graph = self._build_graph()

    # ─────────────────────────── graph wiring ───────────────────────────

    def _build_graph(self):
        g = StateGraph(PipelineState)
        g.add_node("parse", self._parse)
        g.add_node("prefilter", self._prefilter)
        g.add_node("classify", self._classify)
        g.add_node("enrich", self._enrich)
        g.add_node("draft", self._draft)
        g.add_node("route", self._route)
        g.add_node("auto_send", self._auto_send)
        g.add_node("enqueue_approval", self._enqueue_approval)
        g.add_node("escalate", self._escalate)
        g.add_node("drop", self._drop)
        g.add_node("finalize", self._finalize)

        g.set_entry_point("parse")
        g.add_conditional_edges(
            "parse", self._after_parse, {"drop": "drop", "continue": "prefilter"}
        )
        g.add_conditional_edges(
            "prefilter", self._after_prefilter, {"drop": "drop", "continue": "classify"}
        )
        g.add_edge("classify", "enrich")
        g.add_edge("enrich", "draft")
        g.add_edge("draft", "route")
        g.add_conditional_edges(
            "route",
            self._after_route,
            {
                "auto_send": "auto_send",
                "approve": "enqueue_approval",
                "escalate": "escalate",
            },
        )
        g.add_edge("auto_send", "finalize")
        g.add_edge("enqueue_approval", "finalize")
        g.add_edge("escalate", "finalize")
        g.add_edge("drop", END)
        g.add_edge("finalize", END)
        return g.compile()

    # ─────────────────────────── public API ───────────────────────────

    def process(self, raw: RawMessage) -> PipelineState:
        """Run one message through the graph; returns the final state."""
        result = self.graph.invoke({"raw": raw})
        return result  # type: ignore[return-value]

    # ─────────────────────────── nodes ───────────────────────────

    def _parse(self, state: PipelineState) -> dict:
        raw = state["raw"]
        email = self.transport.parse(raw)
        headers = getattr(self.transport, "raw_headers", lambda r: {})(raw)
        # Idempotency (§13.3): already-recorded Message-ID → drop as duplicate.
        if self.messages.already_processed(email.message_id):
            return {
                "email": email,
                "headers": headers,
                "outcome": "no_action",
                "reason": "duplicate message_id (already processed)",
            }
        return {"email": email, "headers": headers}

    def _after_parse(self, state: PipelineState) -> str:
        return "drop" if state.get("reason") else "continue"

    def _prefilter(self, state: PipelineState) -> dict:
        result = prefilter(state["email"], extra_headers=state.get("headers"))
        if result.drop:
            return {"outcome": "no_action", "reason": result.reason}
        return {}

    def _after_prefilter(self, state: PipelineState) -> str:
        return "drop" if state.get("reason") else "continue"

    def _classify(self, state: PipelineState) -> dict:
        classification = classify_stage.classify(
            state["email"], self.llm, self.config.models.triage
        )
        return {"classification": classification}

    def _enrich(self, state: PipelineState) -> dict:
        email = state["email"]
        enrichment = enrich_stage.enrich(
            email, state["classification"], self.crm, self.knowledge
        )
        # Persist the message row now (before any approval references it via FK).
        # Folder is provisional (INBOX); finalize updates it to the final lane.
        contact_id = enrichment.contact.id if enrichment.contact else None
        thread_id = self.messages.ensure_thread(email, contact_id)
        self.messages.record_message(
            email, state["classification"], thread_id, self.config.transport.imap.inbox
        )
        # Attachment offload (§10): offload blobs to the object store (adapter does
        # the disk I/O) and persist references. Only here, post-prefilter, so junk
        # attachments are never stored or cataloged.
        if self.config.retention.offload_attachments and email.attachments:
            offloader = getattr(self.transport, "offload_attachments", None)
            if offloader is not None:
                try:
                    offloader(email, state["raw"].raw_bytes)
                except Exception as e:
                    log.warning("attachment offload failed for %s: %s", email.message_id, e)
            self.messages.record_attachments(email.message_id, email.attachments)
        return {"enrichment": enrichment, "thread_id": thread_id}

    def _draft(self, state: PipelineState) -> dict:
        email = state["email"]
        brand_cfg = self.config.brands.get(email.brand) if email.brand else None
        voice = brand_cfg.voice if brand_cfg else ""
        d = draft_stage.draft_reply(
            email,
            state["classification"],
            state["enrichment"],
            brand_voice=voice,
            llm=self.llm,
            model=self.config.models.draft,
        )
        return {"draft": d}

    def _route(self, state: PipelineState) -> dict:
        rr = route_stage.decide_route(
            state["classification"],
            state["draft"],
            self.config.autonomy,
            effective_mode=self.config.effective_mode(),
        )
        log.info(
            "route msg=%s action=%s reason=%s",
            state["email"].message_id,
            rr.action.value,
            rr.reason,
        )
        return {"route": rr}

    def _after_route(self, state: PipelineState) -> str:
        action = state["route"].action
        if action == RouteAction.AUTO_SEND:
            return "auto_send"
        if action == RouteAction.ESCALATE:
            return "escalate"
        return "approve"

    def _auto_send(self, state: PipelineState) -> dict:
        email = state["email"]
        outgoing = self._build_outgoing(email, state["draft"].body_text)
        self.transport.send(outgoing)
        self.audit.write("agent", "auto_send", email.message_id, {"to": outgoing.to_addr})
        return {"outcome": "auto_sent", "folder": self.config.transport.imap.folders.replied}

    def _enqueue_approval(self, state: PipelineState) -> dict:
        email = state["email"]
        enr = state["enrichment"]
        pending = PendingApproval(
            message_id=email.message_id,
            brand=email.brand,
            to_addr=email.true_sender,
            reply_from=email.to_addr,  # reply AS the address the customer wrote to (§9)
            subject=_reply_subject(email.subject),
            draft=state["draft"],
            classification=state["classification"],
            context={
                "original_subject": email.subject,
                "from": email.true_sender,
                "kb_sources": [c.source for c in state["draft"].used_kb_chunks],
                "history_count": len(getattr(enr, "history", []) or []),
                "route_reason": state["route"].reason,
            },
        )
        approval_id = self.notifier.enqueue_for_approval(pending)
        self.audit.write("agent", "enqueue_approval", email.message_id, {"approval_id": approval_id})
        return {
            "approval_id": approval_id,
            "outcome": "awaiting",
            "folder": self.config.transport.imap.folders.awaiting,
        }

    def _escalate(self, state: PipelineState) -> dict:
        email = state["email"]
        # Escalations still surface the draft for a human, in the Escalated lane.
        enr = state["enrichment"]
        pending = PendingApproval(
            message_id=email.message_id,
            brand=email.brand,
            to_addr=email.true_sender,
            reply_from=email.to_addr,  # reply AS the address the customer wrote to (§9)
            subject=_reply_subject(email.subject),
            draft=state["draft"],
            classification=state["classification"],
            context={
                "escalated": True,
                "route_reason": state["route"].reason,
                "history_count": len(getattr(enr, "history", []) or []),
            },
        )
        approval_id = self.notifier.enqueue_for_approval(pending)
        self.audit.write(
            "agent", "escalate", email.message_id, {"reason": state["route"].reason}
        )
        return {
            "approval_id": approval_id,
            "outcome": "escalated",
            "folder": self.config.transport.imap.folders.escalated,
        }

    def _drop(self, state: PipelineState) -> dict:
        email = state.get("email")
        folder = self.config.transport.imap.folders.no_action
        if email:
            self.audit.write("agent", "no_action", email.message_id, {"reason": state.get("reason")})
        return {"outcome": "no_action", "folder": folder}

    def _finalize(self, state: PipelineState) -> dict:
        """Move out of INBOX, mark processed, persist, log interaction + audit (§7.9)."""
        email = state.get("email")
        if email is None:
            return {}
        folder = state.get("folder", self.config.transport.imap.folders.no_action)

        # The message row was recorded in _enrich for the main path. For drop
        # paths (prefilter/duplicate) it may not exist yet — ensure it does, so
        # idempotency holds. Then update the folder to the final lane.
        contact = getattr(state.get("enrichment"), "contact", None)
        contact_id = contact.id if contact else None
        thread_id = state.get("thread_id")
        if not self.messages.already_processed(email.message_id):
            thread_id = self.messages.ensure_thread(email, contact_id)
            self.messages.record_message(
                email, state.get("classification"), thread_id, folder
            )
        else:
            self.messages.set_folder(email.message_id, folder)

        if contact_id:
            self.crm.log_interaction(
                contact_id,
                Interaction(
                    contact_id=contact_id,
                    thread_id=thread_id,
                    direction=Direction.INBOUND,
                    channel="email",
                    summary=_summarize(email, state.get("outcome")),
                ),
            )

        # Transport side effects: move out of INBOX, flag processed.
        try:
            self.transport.move(email.uid, folder)
            self.transport.mark_processed(email.uid)
        except Exception as e:  # don't lose the DB record if IMAP hiccups
            log.warning("transport finalize failed for %s: %s", email.message_id, e)

        self.audit.write("agent", "finalize", email.message_id,
                         {"outcome": state.get("outcome"), "folder": folder})
        return {}

    # ─────────────────────── approval resumption (Phase 5) ───────────────────────

    def process_approval(self, approval: PendingApproval, decision) -> str:
        """Apply a human decision to a queued draft. Returns the outcome string.

        decision.decision ∈ {send, edit, discard, escalate}. For send/edit we
        build the OutgoingEmail from the (possibly edited) body and the original
        message's threading headers, send as the brand identity, and move the
        source to Replied/.
        """
        from agent.core.models import DecisionType

        folders = self.config.transport.imap.folders
        # Reconstruct threading context from the recorded message row.
        msg_row = self.messages.db.query_one(
            "SELECT * FROM messages WHERE message_id = ?", (approval.message_id,)
        )
        uid = msg_row["uid"] if msg_row else None

        if decision.decision in (DecisionType.DISCARD,):
            self.notifier.resolve(approval.id, decision)
            self.audit.write(decision.actor, "discard", approval.message_id, None)
            return "discarded"

        if decision.decision == DecisionType.ESCALATE:
            self.notifier.resolve(approval.id, decision)
            if uid:
                self._safe_move(uid, folders.escalated)
            self.audit.write(decision.actor, "escalate_manual", approval.message_id, None)
            return "escalated"

        # send or edit → actually send.
        body = decision.edited_body or approval.draft.body_text
        # Reply AS the address the customer originally wrote to, so continued
        # replies route back through the same monitored mailbox (§9). Fall back to
        # the brand's identity only when that recipient isn't a configured identity
        # (e.g. legacy approvals queued before reply_from was persisted).
        reply_from = approval.reply_from
        if not reply_from or self.config.identity_for(reply_from) is None:
            reply_from = _brand_to_addr(self.config, approval.brand)
        outgoing = OutgoingEmail(
            from_identity=reply_from or "",
            to_addr=approval.to_addr,
            subject=approval.subject,
            body_text=body,
            reply_to=None,
            in_reply_to=approval.message_id,
            references=[approval.message_id],
        )
        self.transport.send(outgoing)
        self.notifier.resolve(approval.id, decision)
        if uid:
            self._safe_move(uid, folders.replied)
            self.messages.set_folder(approval.message_id, folders.replied)

        # Log the outbound interaction.
        contact = self.crm.find_contact(approval.to_addr)
        if contact and contact.id:
            self.crm.log_interaction(
                contact.id,
                Interaction(
                    contact_id=contact.id,
                    direction=Direction.OUTBOUND,
                    channel="email",
                    summary=f"Replied: {approval.subject}",
                ),
            )
        self.audit.write(
            decision.actor,
            "send" if decision.decision == DecisionType.SEND else "send_edited",
            approval.message_id,
            {"to": approval.to_addr},
        )
        return "replied"

    # ─────────────────────────── helpers ───────────────────────────

    def _build_outgoing(self, email: Email, body: str) -> OutgoingEmail:
        return OutgoingEmail(
            from_identity=email.to_addr,  # match identity by original recipient (§9)
            to_addr=email.true_sender,
            subject=_reply_subject(email.subject),
            body_text=body,
            in_reply_to=email.message_id,
            references=email.references + [email.message_id],
        )

    def _safe_move(self, uid: str, folder: str) -> None:
        try:
            self.transport.move(uid, folder)
            self.transport.mark_processed(uid)
        except Exception as e:
            log.warning("move uid=%s -> %s failed: %s", uid, folder, e)


def _reply_subject(subject: str) -> str:
    s = subject or ""
    return s if s.lower().startswith("re:") else f"Re: {s}"


def _summarize(email: Email, outcome: str | None) -> str:
    snippet = (email.body_text or "").strip().replace("\n", " ")[:120]
    return f"[{outcome}] {email.subject} — {snippet}"


def _brand_to_addr(config: ScenarioConfig, brand: str | None) -> str | None:
    """Find the sending identity address for a brand (matches on domain substring)."""
    if not brand:
        return None
    for ident in config.sending_identities:
        if brand.lower() in ident.match_to.lower():
            return ident.match_to
    return None