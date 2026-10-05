"""Freigabe-Engine.

Ablauf: Antrag anlegen → Mensch gibt frei → erneute Prüfung (Mail unverändert?) → Ausführen → Nachprüfen.
Regeln:
- Jeder Antrag gilt genau einmal und ist an den exakten Inhalt gebunden (Hash).
- Freigeben darf nur ein Mensch mit Freigaberecht für das Postfach.
- Über Hermes (Gerät) braucht jede Freigabe einen frischen Zwei-Faktor-Code.
- Über die Webseite braucht hohes Risiko einen frischen Zwei-Faktor-Code.
- Nach Abschluss werden Mailinhalte aus dem Antrag gelöscht (Datensparsamkeit).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from . import audit
from .access import AccessDenied, require_mailbox
from .config import get_settings
from .models import Mailbox, Proposal, User, utcnow
from .providers import OutgoingMail, ProviderError, provider_for
from .providers.gmail import split_addresses
from .risk import SourceFlags, assess
from .sanitize import ai_text, detect_injection
from .security import canonical_hash, decrypt, verify_totp

ACTION_LABELS = {
    "send": "Neue Mail senden",
    "reply": "Antworten",
    "forward": "Weiterleiten",
    "archive": "Archivieren",
    "trash": "In den Papierkorb",
    "mark_read": "Als gelesen markieren",
    "mark_unread": "Als ungelesen markieren",
    "spam": "Als Spam markieren",
    "untrash": "Aus dem Papierkorb holen",
    "labels": "Labels ändern",
    "batch": "Sammelaktion",
}
_PROTECTED_LABELS = {"INBOX", "UNREAD", "TRASH", "SPAM", "SENT", "DRAFT", "CHAT"}
SEND_ACTIONS = {"send", "reply", "forward"}
_FINAL = {"rejected", "expired", "executed", "failed", "uncertain", "superseded", "withdrawn"}


class ProposalError(Exception):
    def __init__(self, message: str, *, status: int = 400, code: str = "invalid") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


# ------------------------------------------------------------------ Eingaben

class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _addresses(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        parsed = split_addresses(value)
        if not parsed:
            raise ValueError(f"Ungültige Adresse: {value[:80]}")
        out.extend(parsed)
    seen: set[str] = set()
    return [a for a in out if not (a in seen or seen.add(a))]


class SendIn(_In):
    to: list[str] = Field(min_length=1, max_length=50)
    cc: list[str] = Field(default_factory=list, max_length=50)
    subject: str = Field(max_length=500)
    body: str = Field(max_length=100_000)

    @field_validator("to", "cc")
    @classmethod
    def _norm(cls, value: list[str]) -> list[str]:
        return _addresses(value)


class ReplyIn(_In):
    message_id: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1, max_length=100_000)
    reply_all: bool = False


class ForwardIn(_In):
    message_id: str = Field(min_length=1, max_length=256)
    to: list[str] = Field(min_length=1, max_length=20)
    cc: list[str] = Field(default_factory=list, max_length=20)
    body: str = Field(default="", max_length=20_000)

    @field_validator("to", "cc")
    @classmethod
    def _norm(cls, value: list[str]) -> list[str]:
        return _addresses(value)


class MessageIn(_In):
    message_id: str = Field(min_length=1, max_length=256)


class LabelsIn(_In):
    message_id: str = Field(min_length=1, max_length=256)
    add: list[str] = Field(default_factory=list, max_length=20)
    remove: list[str] = Field(default_factory=list, max_length=20)


class BatchIn(_In):
    operation: Literal["archive", "trash", "mark_read", "spam", "untrash"]
    message_ids: list[str] = Field(min_length=1)


_SCHEMAS: dict[str, type[_In]] = {
    "send": SendIn, "reply": ReplyIn, "forward": ForwardIn, "archive": MessageIn, "trash": MessageIn,
    "mark_read": MessageIn, "mark_unread": MessageIn, "spam": MessageIn, "untrash": MessageIn,
    "labels": LabelsIn, "batch": BatchIn,
}


# ------------------------------------------------------------------ Hilfen

def _flags(detail: Any) -> SourceFlags:
    text = ai_text(html=detail.body_html or None, plain=detail.body_plain)
    injection = detect_injection(text.text, *text.hidden_samples, detail.subject)
    return SourceFlags(hidden_content=text.had_hidden_content, injection=injection)


def _msg_summary(detail: Any) -> dict[str, Any]:
    return {"id": detail.id, "from": detail.from_, "subject": detail.subject, "date": detail.date}


def _re(subject: str, prefix: str) -> str:
    s = subject.strip()
    return s if s.lower().startswith(prefix.lower()) else f"{prefix} {s}".strip()


def _expected_labels(action: str, labels: list[str], payload: dict[str, Any]) -> list[str]:
    result = set(labels)
    if action == "archive":
        result.discard("INBOX")
    elif action == "trash":
        result.add("TRASH")
        result.discard("INBOX")
    elif action == "mark_read":
        result.discard("UNREAD")
    elif action == "mark_unread":
        result.add("UNREAD")
    elif action == "spam":
        result.add("SPAM")
        result.discard("INBOX")
    elif action == "untrash":
        result.discard("TRASH")
    elif action == "labels":
        result |= set(payload["add"])
        result -= set(payload["remove"])
    return sorted(result)


def _readback_ok(action: str, before: list[str], after: list[str], payload: dict[str, Any]) -> bool:
    if action == "untrash":
        # Gmail stellt beim Wiederherstellen die alten Labels selbst wieder her.
        return "TRASH" not in after
    return sorted(after) == _expected_labels(action, before, payload)


def _scrub(p: Proposal) -> None:
    """Mailinhalte entfernen, sobald der Antrag abgeschlossen ist."""
    recipients = len(p.preview.get("to", []) or []) + len(p.preview.get("cc", []) or [])
    p.preview = {
        "action": p.action,
        "action_label": ACTION_LABELS.get(p.action, p.action),
        "mailbox": p.preview.get("mailbox", ""),
        "recipients_count": recipients,
        "count": len(p.preview.get("messages", []) or []) or None,
        "scrubbed": True,
    }
    p.payload = {"scrubbed": True}
    p.note = ""


# ------------------------------------------------------------------ Anlegen

def create_proposal(
    db: Session,
    *,
    user: User,
    mailbox_id: str,
    action: str,
    params: dict[str, Any],
    via: str,
    key_id: str | None = None,
    note: str = "",
) -> Proposal:
    settings = get_settings()
    if action not in _SCHEMAS:
        raise ProposalError(f"Unbekannte Aktion: {action}")
    try:
        mailbox, _ = require_mailbox(db, user, mailbox_id, for_ai=(via == "ai"))
    except AccessDenied as exc:
        raise ProposalError(str(exc), status=403, code="forbidden") from exc

    if action in SEND_ACTIONS and mailbox.send_disabled:
        raise ProposalError(
            "Senden ist für dieses Postfach komplett abgeschaltet. Nur lesen und aufräumen ist möglich.",
            status=403, code="send_disabled",
        )

    since = utcnow() - timedelta(hours=1)
    recent = db.scalar(select(func.count()).select_from(Proposal).where(Proposal.mailbox_id == mailbox.id, Proposal.created_at >= since))
    if (recent or 0) >= settings.proposal_rate_limit_per_hour:
        audit.log(db, "proposal.rate_limited", actor_type=via, actor_id=key_id or user.id, mailbox_id=mailbox.id, action=action)
        raise ProposalError("Zu viele Anträge in der letzten Stunde. Bitte später erneut versuchen.", status=429, code="rate_limited")

    try:
        data = _SCHEMAS[action](**params)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise ProposalError(f"Ungültige Eingabe ({'.'.join(map(str, first['loc']))}): {first['msg']}") from exc

    provider = provider_for(mailbox)
    preview: dict[str, Any] = {"action": action, "action_label": ACTION_LABELS[action], "mailbox": mailbox.address}
    snapshot: Any = None
    recipients: list[str] = []
    attachments = False
    batch_count = 0
    source: SourceFlags | None = None

    try:
        if isinstance(data, SendIn):
            payload = data.model_dump()
            recipients = data.to + data.cc
            preview.update(to=data.to, cc=data.cc, subject=data.subject, body=data.body)

        elif isinstance(data, ReplyIn):
            original = provider.get_message(data.message_id)
            thread = provider.get_thread(original.thread_id)
            latest = thread[-1]
            source = _flags(original)
            own = mailbox.address.lower()
            reply_target = split_addresses(original.reply_to) or split_addresses(original.from_)
            to = [a for a in reply_target if a != own]
            cc: list[str] = []
            if data.reply_all:
                for a in split_addresses(original.to) + split_addresses(original.cc):
                    if a != own and a not in to and a not in cc:
                        cc.append(a)
            if not to:
                to = [a for a in split_addresses(original.to) if a != own]
                if not to:
                    raise ProposalError("Für diese Antwort wurde kein Empfänger gefunden.")
            payload = {
                "message_id": original.id, "thread_id": original.thread_id, "latest_message_id": latest.id,
                "to": to, "cc": cc, "subject": _re(original.subject, "Re:"), "body": data.body,
                "in_reply_to": original.message_id_header, "references": original.references,
            }
            recipients = to + cc
            snapshot = {"latest_message_id": latest.id}
            preview.update(to=to, cc=cc, subject=payload["subject"], body=data.body, original=_msg_summary(original))

        elif isinstance(data, ForwardIn):
            original = provider.get_message(data.message_id)
            source = _flags(original)
            payload = {
                "message_id": original.id, "to": data.to, "cc": data.cc,
                "subject": _re(original.subject, "Fwd:"), "body": data.body,
            }
            recipients = data.to + data.cc
            attachments = True
            snapshot = provider.snapshot(original.id)
            preview.update(
                to=data.to, cc=data.cc, subject=payload["subject"], body=data.body, original=_msg_summary(original),
                attachments=[{"name": "Original-Mail (.eml)"}] + list(original.attachments),
            )

        elif isinstance(data, LabelsIn):
            if not data.add and not data.remove:
                raise ProposalError("Keine Label-Änderung angegeben.")
            bad = (set(data.add) | set(data.remove)) & _PROTECTED_LABELS
            if bad:
                raise ProposalError("Diese Labels haben eigene Aktionen: " + ", ".join(sorted(bad)))
            known = {l["id"]: l["name"] for l in provider.list_labels()}
            unknown = [l for l in data.add + data.remove if l not in known]
            if unknown:
                raise ProposalError("Unbekannte Labels: " + ", ".join(unknown[:5]))
            original = provider.get_message(data.message_id)
            payload = {"message_id": original.id, "add": sorted(set(data.add)), "remove": sorted(set(data.remove))}
            snapshot = provider.snapshot(original.id)
            preview.update(
                original=_msg_summary(original),
                add=[known[l] for l in payload["add"]], remove=[known[l] for l in payload["remove"]],
            )

        elif isinstance(data, MessageIn):
            original = provider.get_message(data.message_id)
            payload = {"message_id": original.id}
            snapshot = provider.snapshot(original.id)
            preview.update(original=_msg_summary(original))

        elif isinstance(data, BatchIn):
            ids = list(dict.fromkeys(data.message_ids))
            if len(ids) > settings.batch_max_messages:
                raise ProposalError(f"Höchstens {settings.batch_max_messages} Mails pro Sammelaktion.")
            snaps, items = [], []
            for mid in ids:
                detail = provider.get_message(mid)
                snaps.append(provider.snapshot(mid))
                items.append(_msg_summary(detail))
            payload = {"operation": data.operation, "message_ids": ids}
            snapshot = snaps
            batch_count = len(ids)
            preview.update(operation=ACTION_LABELS[data.operation], messages=items)
        else:  # pragma: no cover
            raise ProposalError("Nicht unterstützt.")
    except ProviderError as exc:
        raise ProposalError(str(exc), status=502, code="provider") from exc

    level, reasons = assess(
        settings=settings, action=action, mailbox_address=mailbox.address, recipients=recipients,
        attachments=attachments, batch_count=batch_count, source=source,
    )
    proposal = Proposal(
        mailbox_id=mailbox.id,
        requested_by_user_id=user.id,
        requested_via=via,
        requested_key_id=key_id,
        action=action,
        payload=payload,
        preview=preview,
        snapshot=snapshot,
        payload_hash=canonical_hash({"action": action, "mailbox": mailbox.id, "payload": payload}),
        risk_level=level,
        risk_reasons=reasons,
        note=(note or "")[:500],
        expires_at=utcnow() + timedelta(minutes=settings.proposal_ttl_minutes),
    )
    db.add(proposal)
    db.flush()
    audit.log(
        db, "proposal.created", actor_type=via, actor_id=key_id or user.id, mailbox_id=mailbox.id,
        proposal_id=proposal.id, action=action, risk_level=level, recipients_count=len(recipients),
    )
    return proposal


# ------------------------------------------------------------------ Abfragen

def expire_due(db: Session) -> int:
    rows = db.execute(select(Proposal).where(Proposal.status == "pending", Proposal.expires_at < utcnow())).scalars().all()
    for p in rows:
        p.status = "expired"
        _scrub(p)
        audit.log(db, "proposal.expired", actor_type="system", mailbox_id=p.mailbox_id, proposal_id=p.id, action=p.action)
    return len(rows)


def visible_proposals(db: Session, user: User, *, status: str | None = "pending", limit: int = 100) -> list[Proposal]:
    from .models import MailboxAccess

    expire_due(db)
    q = (
        select(Proposal)
        .join(MailboxAccess, MailboxAccess.mailbox_id == Proposal.mailbox_id)
        .where(MailboxAccess.user_id == user.id)
        .order_by(Proposal.created_at.desc())
        .limit(limit)
    )
    if status:
        q = q.where(Proposal.status == status)
    return list(db.execute(q).scalars().all())


def get_visible(db: Session, user: User, proposal_id: str) -> Proposal:
    p = db.get(Proposal, proposal_id)
    if p is None:
        raise ProposalError("Antrag nicht gefunden.", status=404, code="not_found")
    try:
        require_mailbox(db, user, p.mailbox_id)
    except AccessDenied as exc:
        raise ProposalError("Antrag nicht gefunden.", status=404, code="not_found") from exc
    if p.status == "pending" and p.expires_at < utcnow():
        p.status = "expired"
        _scrub(p)
    return p


def public_view(p: Proposal) -> dict[str, Any]:
    return {
        "id": p.id,
        "mailbox_id": p.mailbox_id,
        "action": p.action,
        "action_label": ACTION_LABELS.get(p.action, p.action),
        "status": p.status,
        "risk_level": p.risk_level,
        "risk_reasons": p.risk_reasons,
        "requires_code": True,  # über Hermes immer; Web nur bei hohem Risiko (siehe approve)
        "preview": p.preview,
        "note": p.note,
        "requested_via": p.requested_via,
        "created_at": p.created_at.isoformat(),
        "expires_at": p.expires_at.isoformat(),
        "decided_via": p.decided_via,
        "decided_at": p.decided_at.isoformat() if p.decided_at else None,
        "result": p.result,
        "error": p.error,
    }


# ------------------------------------------------------------------ Entscheiden

def _check_code(db: Session, user: User, code: str | None) -> None:
    if not user.totp_enabled or not user.totp_secret_enc:
        raise ProposalError("Zwei-Faktor ist für dein Konto nicht eingerichtet.", status=403, code="no_2fa")
    if not code:
        raise ProposalError("Bitte den 6-stelligen Code aus deiner Authenticator-App eingeben.", status=403, code="code_required")
    secret = decrypt(user.totp_secret_enc, context=f"totp:{user.id}")
    step = verify_totp(secret, code, user.totp_last_step)
    if step is None:
        audit.log(db, "auth.code_failed", actor_type="user", actor_id=user.id)
        raise ProposalError("Der Code ist falsch oder wurde schon benutzt.", status=403, code="code_invalid")
    user.totp_last_step = step


def reject(db: Session, *, proposal_id: str, user: User, via: str) -> Proposal:
    p = get_visible(db, user, proposal_id)
    try:
        require_mailbox(db, user, p.mailbox_id, approve=True)
    except AccessDenied as exc:
        raise ProposalError(str(exc), status=403, code="forbidden") from exc
    if p.status != "pending":
        raise ProposalError("Dieser Antrag ist bereits abgeschlossen.", status=409, code="not_pending")
    p.status = "rejected"
    p.decided_by_user_id, p.decided_via, p.decided_at = user.id, via, utcnow()
    _scrub(p)
    audit.log(db, "proposal.rejected", actor_type="user", actor_id=user.id, mailbox_id=p.mailbox_id, proposal_id=p.id, action=p.action, channel=via)
    return p


def withdraw(db: Session, *, proposal_id: str, user: User, key_id: str) -> Proposal:
    """Die KI zieht einen eigenen, noch offenen Antrag zurück."""
    p = get_visible(db, user, proposal_id)
    if p.requested_key_id != key_id:
        raise ProposalError("Nur eigene Anträge können zurückgezogen werden.", status=403, code="forbidden")
    if p.status != "pending":
        raise ProposalError("Dieser Antrag ist bereits abgeschlossen.", status=409, code="not_pending")
    p.status = "withdrawn"
    _scrub(p)
    audit.log(db, "proposal.withdrawn", actor_type="ai", actor_id=key_id, mailbox_id=p.mailbox_id, proposal_id=p.id, action=p.action)
    return p


def approve(
    db: Session,
    *,
    proposal_id: str,
    user: User,
    via: Literal["web", "device"],
    code: str | None = None,
    web_second_factor_ok: bool = False,
) -> Proposal:
    if via not in ("web", "device"):
        raise ProposalError("Freigaben sind nur über die Webseite oder Hermes möglich.", status=403, code="forbidden")
    p = get_visible(db, user, proposal_id)
    try:
        require_mailbox(db, user, p.mailbox_id, approve=True)
    except AccessDenied as exc:
        raise ProposalError(str(exc), status=403, code="forbidden") from exc
    if p.status != "pending":
        raise ProposalError("Dieser Antrag ist bereits abgeschlossen oder abgelaufen.", status=409, code="not_pending")

    if via == "device":
        # Hermes läuft in einer Umgebung, die der Agent kontrolliert. Was das Haus verlässt
        # oder riskant ist, wird deshalb nur außerhalb von Hermes freigegeben.
        if p.action in SEND_ACTIONS or p.risk_level == "high":
            raise ProposalError(
                "Diese Aktion kann nur auf der Freigabe-Webseite freigegeben werden (Senden oder hohes Risiko).",
                status=403, code="web_only",
            )
        _check_code(db, user, code)
    else:
        if not web_second_factor_ok:
            raise ProposalError("Bitte zuerst mit Zwei-Faktor anmelden.", status=403, code="no_2fa")
        if p.risk_level == "high":
            _check_code(db, user, code)

    expected = canonical_hash({"action": p.action, "mailbox": p.mailbox_id, "payload": p.payload})
    if expected != p.payload_hash:
        raise ProposalError("Der Antrag wurde verändert und ist ungültig.", status=409, code="tampered")

    # Genau einmal: atomar von pending auf executing umstellen.
    claimed = db.execute(
        update(Proposal).where(Proposal.id == p.id, Proposal.status == "pending").values(
            status="executing", decided_by_user_id=user.id, decided_via=via, decided_at=utcnow()
        )
    ).rowcount
    if claimed != 1:
        raise ProposalError("Dieser Antrag wird bereits bearbeitet.", status=409, code="not_pending")
    db.flush()
    db.refresh(p)
    audit.log(db, "proposal.approved", actor_type="user", actor_id=user.id, mailbox_id=p.mailbox_id, proposal_id=p.id, action=p.action, channel=via, risk_level=p.risk_level)
    _execute(db, p)
    return p


# ------------------------------------------------------------------ Ausführen

def _execute(db: Session, p: Proposal) -> None:
    mailbox: Mailbox = p.mailbox
    payload = p.payload
    mutation_started = False
    try:
        if p.action in SEND_ACTIONS and mailbox.send_disabled:
            return _finish(db, p, "failed", error="Senden ist für dieses Postfach abgeschaltet. Nichts wurde gesendet.")
        provider = provider_for(mailbox)

        # 1) Erneute Prüfung: hat sich die Mail seit dem Antrag verändert?
        if p.action == "reply":
            thread = provider.get_thread(payload["thread_id"])
            if thread[-1].id != payload["latest_message_id"]:
                return _finish(db, p, "superseded", error="In der Unterhaltung ist inzwischen eine neue Mail eingegangen. Bitte neu entscheiden.")
        elif p.action == "batch":
            for mid, snap in zip(payload["message_ids"], p.snapshot, strict=True):
                if provider.snapshot(mid) != snap:
                    return _finish(db, p, "superseded", error="Mindestens eine Mail hat sich verändert. Nichts wurde ausgeführt.")
        elif p.snapshot is not None:
            if provider.snapshot(payload["message_id"]) != p.snapshot:
                return _finish(db, p, "superseded", error="Die Mail hat sich verändert. Nichts wurde ausgeführt.")

        # 2) Ausführen
        result: dict[str, Any] = {}
        if p.action in ("send", "reply", "forward"):
            mail = OutgoingMail(
                to=payload["to"], cc=payload["cc"], subject=payload["subject"], body=payload["body"],
                in_reply_to=payload.get("in_reply_to", ""), references=payload.get("references", ""),
                thread_id=payload.get("thread_id", ""),
            )
            if p.action == "forward":
                mail.forward_raw = provider.get_raw(payload["message_id"])
            mutation_started = True
            sent_id = provider.send(mail)
            result["sent_id"] = sent_id
            ok = provider.message_exists_in_sent(sent_id)
        elif p.action == "batch":
            mutation_started = True
            op = payload["operation"]
            for mid, snap in zip(payload["message_ids"], p.snapshot, strict=True):
                _apply_single(provider, op, mid)
            ok = all(
                _readback_ok(payload["operation"], snap["labels"], provider.snapshot(mid)["labels"], {})
                for mid, snap in zip(payload["message_ids"], p.snapshot, strict=True)
            )
            result["count"] = len(payload["message_ids"])
        else:
            mutation_started = True
            _apply_single(provider, p.action, payload["message_id"], payload)
            ok = _readback_ok(p.action, p.snapshot["labels"], provider.snapshot(payload["message_id"])["labels"], payload)

        # 3) Nachprüfen
        if ok:
            _finish(db, p, "executed", result=result)
        else:
            _finish(db, p, "uncertain", result=result, error="Ausgeführt, aber die Nachprüfung passt nicht. Bitte im Postfach kontrollieren.")
    except ProviderError as exc:
        status = "uncertain" if mutation_started else "failed"
        msg = str(exc) + (" Ob die Aktion ausgeführt wurde, ist unklar. Bitte im Postfach prüfen." if mutation_started else " Nichts wurde ausgeführt.")
        _finish(db, p, status, error=msg)


def _apply_single(provider: Any, action: str, message_id: str, payload: dict[str, Any] | None = None) -> None:
    if action == "archive":
        provider.modify_labels(message_id, [], ["INBOX"])
    elif action == "trash":
        provider.trash(message_id)
    elif action == "mark_read":
        provider.modify_labels(message_id, [], ["UNREAD"])
    elif action == "mark_unread":
        provider.modify_labels(message_id, ["UNREAD"], [])
    elif action == "spam":
        provider.modify_labels(message_id, ["SPAM"], ["INBOX"])
    elif action == "untrash":
        provider.untrash(message_id)
    elif action == "labels" and payload is not None:
        provider.modify_labels(message_id, payload["add"], payload["remove"])
    else:
        raise ProviderError("Unbekannte Aktion.")


def _finish(db: Session, p: Proposal, status: str, *, result: dict[str, Any] | None = None, error: str | None = None) -> None:
    p.status = status
    p.result = result or {}
    p.error = error
    _scrub(p)
    audit.log(
        db, f"proposal.{status}", actor_type="system", mailbox_id=p.mailbox_id, proposal_id=p.id,
        action=p.action, ok=(status == "executed"),
    )
