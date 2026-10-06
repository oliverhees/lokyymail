"""Schnittstelle für das Hermes-Desktop-Plugin (Geräte-Schlüssel).

Wichtig: Der Backend-Teil des Hermes-Plugins läuft im selben Prozess wie der Hermes-Agent.
Der Agent könnte den Geräte-Schlüssel also theoretisch lesen. Deshalb verlangt JEDE Freigabe
über diese Schnittstelle einen frischen Zwei-Faktor-Code aus der Authenticator-App des Menschen.
Den kann die KI nicht erzeugen.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..mail_service import MailError, list_mailboxes, message_for_human, search
from ..proposals import ProposalError, approve, create_proposal, get_visible, public_view, reject, visible_proposals
from .deps import KeyContext, device_key

router = APIRouter(prefix="/api/device", tags=["device"])


def _fail(exc: Exception) -> HTTPException:
    status = getattr(exc, "status", 400)
    return HTTPException(status_code=status, detail=str(exc))


@router.get("/status")
def status(k: KeyContext = Depends(device_key), db: Session = Depends(get_db)) -> dict[str, Any]:
    return {
        "user": {"email": k.user.email, "name": k.user.display_name or k.user.email, "two_factor": k.user.totp_enabled},
        "mailboxes": list_mailboxes(db, k.user, for_ai=False),
        "pending": len(visible_proposals(db, k.user, status="pending")),
        "approvals": {"hermes": get_settings().hermes_approvals},
    }


@router.get("/mailboxes/{mailbox_id}/messages")
def messages(
    mailbox_id: str,
    q: str = Query("", max_length=500),
    max_results: int = Query(25, ge=1, le=50),
    page_token: str | None = Query(None, max_length=2048),
    k: KeyContext = Depends(device_key),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        return search(db, k.user, mailbox_id, q or "in:inbox", max_results, page_token, for_ai=False, actor=k.key.id)
    except MailError as exc:
        raise _fail(exc) from exc


@router.get("/mailboxes/{mailbox_id}/messages/{message_id}")
def message(mailbox_id: str, message_id: str, k: KeyContext = Depends(device_key), db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return message_for_human(db, k.user, mailbox_id, message_id)
    except MailError as exc:
        raise _fail(exc) from exc


class ProposeBody(BaseModel):
    action: str = Field(max_length=32)
    params: dict[str, Any]
    note: str = Field(default="", max_length=500)


@router.post("/mailboxes/{mailbox_id}/proposals")
def propose(mailbox_id: str, body: ProposeBody, k: KeyContext = Depends(device_key), db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        p = create_proposal(db, user=k.user, mailbox_id=mailbox_id, action=body.action, params=body.params, via="device", key_id=k.key.id, note=body.note)
    except ProposalError as exc:
        raise _fail(exc) from exc
    return public_view(p)


@router.get("/proposals")
def proposals(status: str = Query("pending", max_length=16), k: KeyContext = Depends(device_key), db: Session = Depends(get_db)) -> dict[str, Any]:
    items = visible_proposals(db, k.user, status=None if status == "all" else status, limit=100)
    return {"proposals": [public_view(p) for p in items]}


@router.get("/proposals/{proposal_id}")
def proposal(proposal_id: str, k: KeyContext = Depends(device_key), db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return public_view(get_visible(db, k.user, proposal_id))
    except ProposalError as exc:
        raise _fail(exc) from exc


class ApproveBody(BaseModel):
    code: str = Field(min_length=6, max_length=12)


@router.post("/proposals/{proposal_id}/approve")
def proposal_approve(proposal_id: str, body: ApproveBody, k: KeyContext = Depends(device_key), db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        p = approve(db, proposal_id=proposal_id, user=k.user, via="device", code=body.code)
    except ProposalError as exc:
        db.commit()  # Fehlversuche bleiben im Protokoll
        raise _fail(exc) from exc
    return public_view(p)


@router.post("/proposals/{proposal_id}/reject")
def proposal_reject(proposal_id: str, k: KeyContext = Depends(device_key), db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return public_view(reject(db, proposal_id=proposal_id, user=k.user, via="device"))
    except ProposalError as exc:
        raise _fail(exc) from exc
