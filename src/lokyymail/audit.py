"""Protokoll. Regel: niemals Betreff, Text oder Empfänger-Inhalte speichern – nur Metadaten."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .models import AuditEvent

# Nur diese Detail-Felder dürfen ins Protokoll. Alles andere wird verworfen.
_ALLOWED_DETAIL_KEYS = {
    "action", "risk_level", "status", "reason", "channel", "count", "provider",
    "key_kind", "key_name", "role", "ok", "error_code", "tool", "recipients_count",
}


def log(
    db: Session,
    event: str,
    *,
    actor_type: str,
    actor_id: str | None = None,
    mailbox_id: str | None = None,
    proposal_id: str | None = None,
    **detail: Any,
) -> None:
    safe = {k: v for k, v in detail.items() if k in _ALLOWED_DETAIL_KEYS}
    db.add(
        AuditEvent(
            actor_type=actor_type,
            actor_id=actor_id,
            event=event,
            mailbox_id=mailbox_id,
            proposal_id=proposal_id,
            detail=safe,
        )
    )
