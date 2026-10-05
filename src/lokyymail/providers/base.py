"""Gemeinsame Schnittstelle aller Anschlüsse (Gmail heute, Microsoft Graph und IMAP in Phase 4)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


class ProviderError(RuntimeError):
    """Fehler beim Mail-Anbieter. Die Nachricht ist für Menschen gedacht und enthält keine Geheimnisse."""


@dataclass
class MessageSummary:
    id: str
    thread_id: str
    from_: str
    to: str
    subject: str
    date: str
    snippet: str
    labels: list[str] = field(default_factory=list)
    unread: bool = False
    has_attachments: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["from"] = data.pop("from_")
        return data


@dataclass
class MessageDetail(MessageSummary):
    cc: str = ""
    reply_to: str = ""
    message_id_header: str = ""
    references: str = ""
    body_plain: str = ""
    body_html: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)  # part_id, Name, Typ, Größe


@dataclass
class OutgoingMail:
    to: list[str]
    cc: list[str]
    subject: str
    body: str
    in_reply_to: str = ""
    references: str = ""
    thread_id: str = ""
    forward_raw: bytes | None = None  # Original als Anhang (Weiterleitung)
    forward_name: str = "weitergeleitet.eml"


class MailProvider(Protocol):
    address: str

    def search(self, query: str, max_results: int, page_token: str | None) -> tuple[list[MessageSummary], str | None]: ...
    def get_message(self, message_id: str) -> MessageDetail: ...
    def get_thread(self, thread_id: str) -> list[MessageDetail]: ...
    def get_raw(self, message_id: str) -> bytes: ...
    def list_labels(self) -> list[dict[str, str]]: ...
    def snapshot(self, message_id: str) -> dict[str, Any]: ...
    def modify_labels(self, message_id: str, add: list[str], remove: list[str]) -> None: ...
    def trash(self, message_id: str) -> None: ...
    def untrash(self, message_id: str) -> None: ...
    def get_attachment(self, message_id: str, part_id: str) -> tuple[bytes, str, str]: ...
    def send(self, mail: OutgoingMail) -> str: ...
    def message_exists_in_sent(self, message_id: str) -> bool: ...


def build_mime(sender: str, mail: OutgoingMail) -> bytes:
    """Baut eine saubere MIME-Nachricht (UTF-8, kein HTML, keine Header-Injection)."""
    from email.message import EmailMessage
    from email.policy import SMTP
    from email.utils import formatdate, make_msgid

    for value in [sender, mail.subject, *mail.to, *mail.cc, mail.in_reply_to, mail.references]:
        if "\r" in value or "\n" in value:
            raise ProviderError("Unzulässige Zeichen in Kopfzeilen.")
    msg = EmailMessage(policy=SMTP)
    msg["From"] = sender
    msg["To"] = ", ".join(mail.to)
    if mail.cc:
        msg["Cc"] = ", ".join(mail.cc)
    msg["Subject"] = mail.subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1] if "@" in sender else None)
    if mail.in_reply_to:
        msg["In-Reply-To"] = mail.in_reply_to
        msg["References"] = (mail.references + " " + mail.in_reply_to).strip()
    msg.set_content(mail.body, charset="utf-8")
    if mail.forward_raw is not None:
        msg.add_attachment(mail.forward_raw, maintype="message", subtype="rfc822", filename=mail.forward_name)
    return msg.as_bytes()
