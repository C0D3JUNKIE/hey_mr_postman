"""cPanel IMAP/SMTP transport adapter (§9).

Implements TransportPort over IMAP (read) + SMTP (send). The MIME parsing is
written as pure module-level functions over raw bytes so it can be unit-tested
against .eml fixtures with no live mailbox.

This is an ADAPTER: it is allowed to import imaplib/smtplib. The core never does.

true_sender resolution (§6) — the linchpin for forwarded contact-form mail:
  1. Reply-To header (forwarders set this to the real customer).
  2. Body-regex fallback for form-mailer payloads ("Email: x@y.com", etc.).
  3. From header (last resort).
"""

from __future__ import annotations

import email as email_lib
import imaplib
import logging
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import getaddresses, parsedate_to_datetime

from agent.config import ScenarioConfig, resolve_secret
from agent.core.models import Attachment, Email, OutgoingEmail, RawMessage

log = logging.getLogger(__name__)


# ─────────────────────── pure MIME parsing (testable) ───────────────────────

# Form-mailer payloads commonly embed the real sender in the body.
_BODY_EMAIL_PATTERNS = (
    re.compile(r"^\s*(?:e-?mail|from|reply[- ]?to|sender)\s*[:=]\s*(?P<addr>[^\s<>]+@[^\s<>]+)", re.I | re.M),
    re.compile(r"<(?P<addr>[^\s<>]+@[^\s<>]+)>"),
    re.compile(r"(?P<addr>[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})", re.IGNORECASE),
)

_EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)


def _first_addr(value: str | None) -> str | None:
    if not value:
        return None
    addrs = [a for _, a in getaddresses([value]) if a]
    return addrs[0] if addrs else None


def _extract_bodies(msg: EmailMessage) -> tuple[str, str | None]:
    """Return (text, html|None) walking the MIME tree."""
    text_parts: list[str] = []
    html_parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            try:
                content = part.get_content()
            except Exception:  # undecodable part — skip
                continue
            if ctype == "text/plain":
                text_parts.append(content)
            elif ctype == "text/html":
                html_parts.append(content)
    else:
        ctype = msg.get_content_type()
        try:
            content = msg.get_content()
        except Exception:
            content = ""
        if ctype == "text/html":
            html_parts.append(content)
        else:
            text_parts.append(content)
    text = "\n".join(p for p in text_parts if p).strip()
    html = "\n".join(p for p in html_parts if p).strip() or None
    return text, html


def _extract_attachments(msg: EmailMessage) -> list[Attachment]:
    out: list[Attachment] = []
    if not msg.is_multipart():
        return out
    for part in msg.walk():
        disp = (part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        if "attachment" not in disp and not filename:
            continue
        payload = part.get_payload(decode=True) or b""
        out.append(
            Attachment(
                filename=filename or "attachment",
                content_type=part.get_content_type(),
                size=len(payload),
            )
        )
    return out


def resolve_true_sender(
    from_addr: str | None, reply_to: str | None, body_text: str
) -> str:
    """Best-effort recovery of the REAL customer address (§6)."""
    # 1) Reply-To wins for forwarded form mail.
    rt = _first_addr(reply_to)
    if rt:
        return rt
    # 2) Body-regex fallback for form-mailer payloads.
    for pat in _BODY_EMAIL_PATTERNS:
        m = pat.search(body_text or "")
        if m:
            addr = m.group("addr").strip().strip(".,;:")
            # Don't pick up the forwarder's own no-reply address from the body.
            if _EMAIL_RE.fullmatch(addr):
                return addr
    # 3) From header.
    return from_addr or ""


def resolve_brand(to_addr: str, config: ScenarioConfig) -> str | None:
    """Infer brand key from the recipient address.

    v1 heuristic: a brand key (siteA, siteB, ...) that appears in the to_addr
    domain wins. Keeps config bundle-shaped without a separate mapping table.
    """
    target = (to_addr or "").lower()
    for brand_key in config.brands:
        if brand_key.lower() in target:
            return brand_key
    return None


def parse_raw(raw_bytes: bytes, uid: str, folder: str, config: ScenarioConfig) -> Email:
    """MIME bytes → normalized Email. Pure; safe to call in tests."""
    msg: EmailMessage = email_lib.message_from_bytes(raw_bytes, policy=default_policy)

    from_addr = _first_addr(msg.get("From")) or ""
    reply_to = _first_addr(msg.get("Reply-To"))
    to_addr = _first_addr(msg.get("To")) or ""
    subject = str(msg.get("Subject") or "")
    message_id = (msg.get("Message-ID") or "").strip() or f"<no-id-{uid}@local>"
    in_reply_to = (msg.get("In-Reply-To") or "").strip() or None
    references = [r for r in re.split(r"\s+", msg.get("References", "").strip()) if r]

    received_at = None
    if msg.get("Date"):
        try:
            received_at = parsedate_to_datetime(msg.get("Date"))
        except (TypeError, ValueError):
            received_at = None

    body_text, body_html = _extract_bodies(msg)
    true_sender = resolve_true_sender(from_addr, reply_to, body_text)
    brand = resolve_brand(to_addr, config)

    return Email(
        uid=uid,
        message_id=message_id,
        brand=brand,
        from_addr=from_addr,
        true_sender=true_sender,
        reply_to=reply_to,
        to_addr=to_addr,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        in_reply_to=in_reply_to,
        references=references,
        attachments=_extract_attachments(msg),
        received_at=received_at,
    )


def extract_raw_headers(raw_bytes: bytes) -> dict[str, str]:
    """Pull the headers the prefilter cares about (List-Id, Auto-Submitted, ...)."""
    msg = email_lib.message_from_bytes(raw_bytes, policy=default_policy)
    wanted = (
        "auto-submitted",
        "x-auto-response-suppress",
        "list-id",
        "list-unsubscribe",
        "precedence",
        "x-autoreply",
        "x-autorespond",
    )
    return {h: str(msg.get(h)) for h in wanted if msg.get(h) is not None}


# ─────────────────────────── the adapter ───────────────────────────


class ImapSmtpTransport:
    """TransportPort over cPanel IMAP + SMTP. Connections are opened lazily."""

    def __init__(self, config: ScenarioConfig):
        self.config = config
        self.imap_cfg = config.transport.imap
        self.folders = self.imap_cfg.folders
        self._imap: imaplib.IMAP4 | None = None

    # ── IMAP connection management ──

    def _connect_imap(self) -> imaplib.IMAP4:
        if self._imap is not None:
            try:
                self._imap.noop()
                return self._imap
            except Exception:
                self._imap = None
        password = resolve_secret(self.imap_cfg.password_env)
        if self.imap_cfg.ssl:
            conn = imaplib.IMAP4_SSL(self.imap_cfg.host, self.imap_cfg.port)
        else:
            conn = imaplib.IMAP4(self.imap_cfg.host, self.imap_cfg.port)
        conn.login(self.imap_cfg.username, password)
        self._ensure_folders(conn)
        self._imap = conn
        return conn

    def _ensure_folders(self, conn: imaplib.IMAP4) -> None:
        """Create the status folders if missing (§10). Idempotent."""
        for name in (
            self.folders.replied,
            self.folders.awaiting,
            self.folders.escalated,
            self.folders.no_action,
            self.folders.trash,
        ):
            try:
                conn.create(name)
            except Exception:
                pass  # already exists

    # ── TransportPort: read ──

    def read_new(self) -> list[RawMessage]:
        """SEARCH UNSEEN in INBOX, fetch by UID, return RawMessage list."""
        conn = self._connect_imap()
        conn.select(self.imap_cfg.inbox)
        typ, data = conn.uid("SEARCH", None, "UNSEEN")
        if typ != "OK":
            log.warning("IMAP SEARCH failed: %s", typ)
            return []
        uids = data[0].split() if data and data[0] else []
        out: list[RawMessage] = []
        for uid_bytes in uids:
            uid = uid_bytes.decode()
            typ, msg_data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not msg_data[0]:
                log.warning("IMAP FETCH failed for uid %s", uid)
                continue
            raw_bytes = msg_data[0][1]
            out.append(RawMessage(uid=uid, folder=self.imap_cfg.inbox, raw_bytes=raw_bytes))
        return out

    def parse(self, raw: RawMessage) -> Email:
        return parse_raw(raw.raw_bytes, raw.uid, raw.folder, self.config)

    def raw_headers(self, raw: RawMessage) -> dict[str, str]:
        """Adapter extra: prefilter headers from the raw bytes (not in the port)."""
        return extract_raw_headers(raw.raw_bytes)

    # ── TransportPort: send ──

    def send(self, reply: OutgoingEmail) -> None:
        """Send as the matched brand identity over SMTP_SSL (§9)."""
        identity = self.config.identity_for(reply.from_identity)
        if identity is None:
            raise ValueError(
                f"no sending identity configured for {reply.from_identity!r}; "
                "refusing to send from the wrong server (DMARC)."
            )
        msg = EmailMessage()
        msg["From"] = identity.username
        msg["To"] = reply.to_addr
        msg["Subject"] = reply.subject
        if reply.reply_to:
            msg["Reply-To"] = reply.reply_to
        if reply.in_reply_to:
            msg["In-Reply-To"] = reply.in_reply_to
        if reply.references:
            msg["References"] = " ".join(reply.references)
        msg.set_content(reply.body_text)

        password = resolve_secret(identity.password_env)
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(identity.host, identity.port, context=context) as smtp:
            smtp.login(identity.username, password)
            smtp.send_message(msg)
        log.info("sent reply to %s as %s", reply.to_addr, identity.username)

    # ── TransportPort: folder / idempotency ──

    def move(self, msg_uid: str, folder: str) -> None:
        """Move a message out of INBOX into a status folder (§10)."""
        conn = self._connect_imap()
        conn.select(self.imap_cfg.inbox)
        # UID MOVE (RFC 6851) when available; else COPY + \Deleted.
        try:
            typ, _ = conn.uid("MOVE", msg_uid, folder)
            if typ == "OK":
                return
        except Exception:
            pass
        conn.uid("COPY", msg_uid, folder)
        conn.uid("STORE", msg_uid, "+FLAGS", "(\\Deleted)")
        conn.expunge()

    def mark_processed(self, msg_uid: str) -> None:
        """Flag \\Seen as a belt-and-suspenders processed marker (§13.3).

        Folder membership is the primary idempotency signal; this is the backup.
        """
        conn = self._connect_imap()
        conn.select(self.imap_cfg.inbox)
        conn.uid("STORE", msg_uid, "+FLAGS", "(\\Seen)")

    def mailbox_usage(self) -> tuple[int, int] | None:
        """(used_bytes, quota_bytes) via IMAP QUOTA, or None if unsupported (§10)."""
        conn = self._connect_imap()
        try:
            typ, data = conn.getquotaroot(self.imap_cfg.inbox)
            if typ != "OK":
                return None
            for entry in data:
                m = re.search(rb"STORAGE (\d+) (\d+)", entry if isinstance(entry, bytes) else b"")
                if m:
                    return int(m.group(1)) * 1024, int(m.group(2)) * 1024
        except Exception:
            return None
        return None

    def close(self) -> None:
        if self._imap is not None:
            try:
                self._imap.logout()
            except Exception:
                pass
            self._imap = None