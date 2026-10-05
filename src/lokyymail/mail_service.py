"""Lesen von Mails – getrennt für die KI (entschärft, verpackt) und für Menschen (gefiltertes HTML)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from . import audit
from .access import AccessDenied, accessible_mailboxes, require_mailbox
from .models import User
from .providers import MessageDetail, ProviderError, provider_for
from .sanitize import ai_text, detect_injection, injection_labels, safe_display_html, wrap_for_ai


class MailError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _mailbox(db: Session, user: User, mailbox_id: str, for_ai: bool):
    try:
        mailbox, access = require_mailbox(db, user, mailbox_id, for_ai=for_ai)
    except AccessDenied as exc:
        raise MailError(str(exc), 403) from exc
    try:
        return mailbox, access, provider_for(mailbox)
    except ProviderError as exc:
        raise MailError(str(exc), 502) from exc


def list_mailboxes(db: Session, user: User, *, for_ai: bool) -> list[dict[str, Any]]:
    return [
        {
            "id": m.id,
            "address": m.address,
            "provider": m.provider,
            "role": a.role,
            "can_approve": a.can_approve,
            "ai_enabled": m.ai_enabled,
            "send_disabled": m.send_disabled,
            "status": m.status,
        }
        for m, a in accessible_mailboxes(db, user, for_ai=for_ai)
    ]


def search(db: Session, user: User, mailbox_id: str, query: str, max_results: int, page_token: str | None, *, for_ai: bool, actor: str) -> dict[str, Any]:
    mailbox, _, provider = _mailbox(db, user, mailbox_id, for_ai)
    try:
        items, next_token = provider.search(query, max(1, min(max_results, 50)), page_token)
    except ProviderError as exc:
        raise MailError(str(exc), 502) from exc
    if for_ai:
        audit.log(db, "ai.search", actor_type="ai", actor_id=actor, mailbox_id=mailbox.id, count=len(items))
    out = []
    for m in items:
        d = m.to_dict()
        if for_ai:
            # Betreff und Vorschau sind fremder Inhalt: Steuerzeichen raus, Injection markieren.
            subj = ai_text(html=None, plain=m.subject).text
            snip = ai_text(html=None, plain=m.snippet).text
            d.update(subject=subj, snippet=snip, injection_warning=bool(detect_injection(subj, snip)))
        out.append(d)
    return {"mailbox": mailbox.address, "messages": out, "next_page_token": next_token}


def _detail(db: Session, user: User, mailbox_id: str, message_id: str, for_ai: bool) -> tuple[Any, MessageDetail]:
    mailbox, _, provider = _mailbox(db, user, mailbox_id, for_ai)
    try:
        return mailbox, provider.get_message(message_id)
    except ProviderError as exc:
        raise MailError(str(exc), 502) from exc


def message_for_ai(db: Session, user: User, mailbox_id: str, message_id: str, *, actor: str) -> str:
    mailbox, d = _detail(db, user, mailbox_id, message_id, True)
    audit.log(db, "ai.read", actor_type="ai", actor_id=actor, mailbox_id=mailbox.id, count=1)
    return _wrap(d)


def thread_for_ai(db: Session, user: User, mailbox_id: str, thread_id: str, *, actor: str) -> str:
    mailbox, _, provider = _mailbox(db, user, mailbox_id, True)
    try:
        items = provider.get_thread(thread_id)[-20:]
    except ProviderError as exc:
        raise MailError(str(exc), 502) from exc
    audit.log(db, "ai.read", actor_type="ai", actor_id=actor, mailbox_id=mailbox.id, count=len(items))
    return "\n\n".join(_wrap(d) for d in items)


def _wrap(d: MessageDetail) -> str:
    body = ai_text(html=d.body_html or None, plain=d.body_plain)
    subject = ai_text(html=None, plain=d.subject).text
    inj = detect_injection(body.text, *body.hidden_samples, subject)
    header = {
        "Nachrichten-ID": d.id,
        "Unterhaltung": d.thread_id,
        "Von": d.from_,
        "An": d.to,
        "Cc": d.cc,
        "Datum": d.date,
        "Betreff": subject,
        "Labels": ", ".join(d.labels),
        "Anhänge": ", ".join(f"{a['name']} (part_id {a.get('part_id', '?')})" for a in d.attachments) or "keine",
    }
    return wrap_for_ai(header={k: v for k, v in header.items() if v}, body=body, injection=inj)


def attachment_for_ai(db: Session, user: User, mailbox_id: str, message_id: str, part_id: str, *, actor: str) -> str:
    from .attachments import MAX_CHARS, extract_text

    mailbox, _, provider = _mailbox(db, user, mailbox_id, True)
    try:
        data, name, mime = provider.get_attachment(message_id, part_id)
    except ProviderError as exc:
        raise MailError(str(exc), 502) from exc
    text, hint = extract_text(data, name, mime)
    audit.log(db, "ai.read_attachment", actor_type="ai", actor_id=actor, mailbox_id=mailbox.id, count=1)
    if text is None:
        return f"Anhang '{name}': NICHT_LESBAR. {hint}"
    body = ai_text(html=None, plain=text, limit=MAX_CHARS)
    header = {"Anhang": name, "Typ": mime, "Nachrichten-ID": message_id}
    if hint:
        header["Hinweis"] = hint
    return wrap_for_ai(header=header, body=body, injection=detect_injection(body.text))


def message_for_human(db: Session, user: User, mailbox_id: str, message_id: str) -> dict[str, Any]:
    _, d = _detail(db, user, mailbox_id, message_id, False)
    body = ai_text(html=d.body_html or None, plain=d.body_plain)
    inj = detect_injection(body.text, *body.hidden_samples, d.subject)
    data = d.to_dict()
    data.pop("body_html", None)
    data.pop("body_plain", None)
    data.update(
        cc=d.cc,
        text=body.text,
        html=safe_display_html(d.body_html) if d.body_html else "",
        attachments=d.attachments,
        warnings=(
            (["Diese Mail enthält versteckten Text. In der HTML-Ansicht wird er sichtbar."] if body.had_hidden_content else [])
            + injection_labels(inj)
        ),
    )
    return data
