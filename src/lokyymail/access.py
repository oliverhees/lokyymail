"""Zugriffsregeln. Jede Lese- und Schreibaktion läuft hier durch."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Mailbox, MailboxAccess, User


class AccessDenied(Exception):
    pass


def accessible_mailboxes(db: Session, user: User, *, for_ai: bool = False) -> list[tuple[Mailbox, MailboxAccess]]:
    rows = db.execute(
        select(Mailbox, MailboxAccess)
        .join(MailboxAccess, MailboxAccess.mailbox_id == Mailbox.id)
        .where(MailboxAccess.user_id == user.id)
        .order_by(Mailbox.address)
    ).all()
    out = []
    for mailbox, access in rows:
        if for_ai and (not mailbox.ai_enabled or mailbox.status != "active"):
            continue
        out.append((mailbox, access))
    return out


def require_mailbox(db: Session, user: User, mailbox_id: str, *, for_ai: bool = False, approve: bool = False) -> tuple[Mailbox, MailboxAccess]:
    access = db.execute(
        select(MailboxAccess).where(MailboxAccess.user_id == user.id, MailboxAccess.mailbox_id == mailbox_id)
    ).scalar_one_or_none()
    if access is None:
        raise AccessDenied("Kein Zugriff auf dieses Postfach.")
    mailbox = access.mailbox
    if for_ai and not mailbox.ai_enabled:
        raise AccessDenied("Die KI hat für dieses Postfach keinen Zugriff. Freischalten unter Postfächer.")
    if for_ai and mailbox.status != "active":
        raise AccessDenied("Dieses Postfach ist gerade nicht verbunden.")
    if approve and not access.can_approve:
        raise AccessDenied("Du darfst Aktionen für dieses Postfach nicht freigeben.")
    return mailbox, access
