"""Domain models — the shared vocabulary of the agent core.

These are pure data: no I/O, no adapter imports. Every layer (ports, adapters,
core pipeline) speaks in terms of these types. Kept as pydantic models so config
loading, LLM JSON parsing, and persistence all get validation for free.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


# ─────────────────────────── enums ────────────────────────────


class Category(str, Enum):
    """Coarse intent buckets. Drives autonomy/routing (§8)."""

    FAQ = "faq"
    ACKNOWLEDGEMENT = "acknowledgement"
    SUPPORT = "support"
    SALES = "sales"
    BILLING = "billing"
    LEGAL = "legal"
    REFUND = "refund"
    SPAM = "spam"
    OTHER = "other"


class Priority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class Sentiment(str, Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


class Direction(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class DecisionType(str, Enum):
    SEND = "send"
    EDIT = "edit"
    DISCARD = "discard"
    ESCALATE = "escalate"


class RouteAction(str, Enum):
    """Output of the route node (§7.7)."""

    AUTO_SEND = "auto_send"
    APPROVE = "approve"
    ESCALATE = "escalate"


# ──────────────────────── transport models ────────────────────────


class Attachment(BaseModel):
    filename: str
    content_type: str
    size: int
    # In v1 we offload the blob and keep a reference (§10). May be unset before offload.
    storage_ref: str | None = None


class RawMessage(BaseModel):
    """Untouched message as pulled from the transport, before MIME parsing."""

    uid: str
    folder: str
    raw_bytes: bytes


class Email(BaseModel):
    """Normalized, transport-agnostic inbound message."""

    uid: str
    message_id: str
    brand: str | None = None
    from_addr: str
    # The REAL customer address. Prefer Reply-To / body over From, because
    # forwarded contact-form mail rewrites From to the website's own system (§6).
    true_sender: str
    reply_to: str | None = None
    to_addr: str
    subject: str = ""
    body_text: str = ""
    body_html: str | None = None
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    received_at: datetime | None = None


class OutgoingEmail(BaseModel):
    """A reply ready to be sent as a specific brand identity."""

    from_identity: str  # the mailbox/address we authenticate & send as
    to_addr: str
    subject: str
    body_text: str
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    reply_to: str | None = None


# ──────────────────────── classification / draft ────────────────────────


class Classification(BaseModel):
    category: Category
    priority: Priority = Priority.NORMAL
    sentiment: Sentiment = Sentiment.NEUTRAL
    language: str = "en"
    needs_human: bool = False
    confidence: float = Field(ge=0.0, le=1.0)


class KBChunk(BaseModel):
    brand: str
    text: str
    source: str
    score: float = 0.0


class Draft(BaseModel):
    body_text: str
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    used_kb_chunks: list[KBChunk] = Field(default_factory=list)


# ──────────────────────── CRM models ────────────────────────


class Contact(BaseModel):
    id: str | None = None
    email: str
    name: str | None = None
    brand: str | None = None
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    tags: list[str] = Field(default_factory=list)


class Interaction(BaseModel):
    id: str | None = None
    contact_id: str
    thread_id: str | None = None
    direction: Direction
    channel: str = "email"
    summary: str = ""
    ts: datetime | None = None


# ──────────────────────── approval models ────────────────────────


class PendingApproval(BaseModel):
    id: str | None = None
    message_id: str
    brand: str | None = None
    to_addr: str
    subject: str
    draft: Draft
    classification: Classification | None = None
    # Free-form context surfaced to the human reviewer (history, KB sources, etc.)
    context: dict = Field(default_factory=dict)
    created_at: datetime | None = None


class ApprovalDecision(BaseModel):
    decision: DecisionType
    # When decision == EDIT, the reviewer's replacement body.
    edited_body: str | None = None
    note: str | None = None
    actor: str = "human"