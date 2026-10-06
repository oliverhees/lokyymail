"""Server-gerenderte Oberfläche (ohne JavaScript-Pflicht). Hier geben Menschen Anträge frei."""

from __future__ import annotations

import csv
import io
from datetime import timedelta
from typing import Any

import segno
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..access import AccessDenied, accessible_mailboxes, require_mailbox
from ..config import get_settings
from ..db import get_db
from ..mail_service import MailError, message_for_human, search
from ..models import ApiKey, AuditEvent, Mailbox, MailboxAccess, OAuthState, User, WebSession, utcnow
from ..proposals import (
    ACTION_LABELS, ProposalError, approve, create_proposal, get_visible, list_auto_actions, public_view, reject, undo_auto,
    visible_proposals,
)
from .. import telegram as tg
from ..providers import credentials_context
from ..providers.base import ProviderError
from ..security import (
    decrypt,
    encrypt,
    encrypt_json,
    hash_password,
    new_api_key,
    new_token,
    new_totp_secret,
    password_problems,
    totp_uri,
    verify_password,
    verify_totp,
)
from .deps import (
    PRE_CSRF_COOKIE,
    SESSION_COOKIE,
    WebContext,
    check_csrf,
    check_pre_csrf,
    client_ip,
    code_limiter,
    create_session,
    load_session,
    login_limiter,
    set_cookie,
    templates,
    web_admin,
    web_user,
)

router = APIRouter()


def render(request: Request, name: str, ctx: WebContext | None = None, status: int = 200, **data: Any) -> HTMLResponse:
    settings = get_settings()
    base = {
        "user": ctx.user if ctx else None, "csrf": ctx.csrf if ctx else "", "labels": ACTION_LABELS, "demo": settings.demo_mode,
        "tg": {"available": bool(settings.telegram_bot_token), "linked": bool(ctx and ctx.user.telegram_chat_id)},
    }
    return templates.TemplateResponse(request, name, {**base, **data}, status_code=status)


def _external(view: dict[str, Any]) -> list[str]:
    from ..risk import external_recipients, org_domains

    pv = view.get("preview") or {}
    addresses = list(pv.get("to") or []) + list(pv.get("cc") or [])
    return external_recipients(addresses, org_domains(get_settings(), pv.get("mailbox", "")))


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ================================================================== Anmeldung

@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    token = new_token(16)
    resp = render(request, "login.html", pre_csrf=token)
    set_cookie(resp, PRE_CSRF_COOKIE, token, max_age=1800)
    return resp


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), pre_csrf: str = Form(""), db: Session = Depends(get_db)):
    check_pre_csrf(request, pre_csrf)
    email = email.strip().lower()
    if not login_limiter.hit(f"{client_ip(request)}|{email}"):
        return render(request, "login.html", pre_csrf=pre_csrf, error="Zu viele Versuche. Bitte 15 Minuten warten.", status=429)
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None or user.disabled or not verify_password(user.password_hash, password):
        audit.log(db, "auth.login_failed", actor_type="user", actor_id=user.id if user else None)
        return render(request, "login.html", pre_csrf=pre_csrf, error="E-Mail oder Passwort ist falsch.", status=401)
    token = create_session(db, user)
    audit.log(db, "auth.password_ok", actor_type="user", actor_id=user.id)
    resp = redirect("/login/code" if user.totp_enabled else "/setup/2fa")
    # "lax": Der Rücksprung von Google (/mailboxes/google/callback) ist eine fremde Navigation und
    # würde mit "strict" ohne Sitzung ankommen (Endlosschleife zum Login). Schreibende Aufrufe sind per CSRF-Token geschützt.
    set_cookie(resp, SESSION_COOKIE, token, max_age=get_settings().session_hours * 3600, samesite="lax")
    resp.delete_cookie(PRE_CSRF_COOKIE, path="/")
    return resp


@router.get("/login/code", response_class=HTMLResponse)
def code_form(request: Request, db: Session = Depends(get_db)):
    ctx = load_session(request, db)
    if ctx is None:
        return redirect("/login")
    if not ctx.user.totp_enabled:
        return redirect("/setup/2fa")
    return render(request, "code.html", ctx)


@router.post("/login/code")
def code_submit(request: Request, code: str = Form(...), csrf: str = Form(""), db: Session = Depends(get_db)):
    ctx = load_session(request, db)
    if ctx is None:
        return redirect("/login")
    check_csrf(ctx, csrf)
    if not code_limiter.hit(f"{client_ip(request)}|{ctx.user.id}"):
        return render(request, "code.html", ctx, error="Zu viele Versuche. Bitte 15 Minuten warten.", status=429)
    secret = decrypt(ctx.user.totp_secret_enc or "", context=f"totp:{ctx.user.id}") if ctx.user.totp_secret_enc else ""
    step = verify_totp(secret, code, ctx.user.totp_last_step) if secret else None
    if step is None:
        audit.log(db, "auth.code_failed", actor_type="user", actor_id=ctx.user.id)
        return render(request, "code.html", ctx, error="Der Code stimmt nicht.", status=401)
    ctx.user.totp_last_step = step
    ctx.session.second_factor_ok = True
    audit.log(db, "auth.login", actor_type="user", actor_id=ctx.user.id)
    return redirect("/")


@router.get("/setup/2fa", response_class=HTMLResponse)
def setup_2fa(request: Request, db: Session = Depends(get_db)):
    ctx = load_session(request, db)
    if ctx is None:
        return redirect("/login")
    if ctx.user.totp_enabled:
        return redirect("/")
    if not ctx.user.totp_secret_enc:
        ctx.user.totp_secret_enc = encrypt(new_totp_secret(), context=f"totp:{ctx.user.id}")
    secret = decrypt(ctx.user.totp_secret_enc, context=f"totp:{ctx.user.id}")
    qr = segno.make(totp_uri(secret, ctx.user.email), error="m")
    width, height = qr.symbol_size(scale=5)
    # viewBox nötig, sonst schneidet das CSS (220px) den festen 185px-Code ab
    qr_svg = qr.svg_inline(scale=5, dark="#18202b", light=None).replace("<svg ", f'<svg viewBox="0 0 {width} {height}" ', 1)
    return render(request, "setup_2fa.html", ctx, qr_svg=qr_svg, secret=secret)


@router.post("/setup/2fa")
def setup_2fa_submit(request: Request, code: str = Form(...), csrf: str = Form(""), db: Session = Depends(get_db)):
    ctx = load_session(request, db)
    if ctx is None:
        return redirect("/login")
    check_csrf(ctx, csrf)
    if not ctx.user.totp_secret_enc:
        return redirect("/setup/2fa")
    secret = decrypt(ctx.user.totp_secret_enc, context=f"totp:{ctx.user.id}")
    step = verify_totp(secret, code, ctx.user.totp_last_step)
    if step is None:
        return redirect("/setup/2fa?fehler=1")
    ctx.user.totp_enabled = True
    ctx.user.totp_last_step = step
    ctx.session.second_factor_ok = True
    audit.log(db, "auth.2fa_enabled", actor_type="user", actor_id=ctx.user.id)
    return redirect("/")


@router.post("/logout")
def logout(request: Request, csrf: str = Form(""), db: Session = Depends(get_db)):
    ctx = load_session(request, db)
    if ctx is not None:
        check_csrf(ctx, csrf)
        db.delete(ctx.session)
        audit.log(db, "auth.logout", actor_type="user", actor_id=ctx.user.id)
    resp = redirect("/login")
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# ================================================================== Übersicht und Anträge

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, ctx: WebContext = Depends(web_user), db: Session = Depends(get_db)):
    pending = [public_view(p) for p in visible_proposals(db, ctx.user, status="pending")]
    boxes = accessible_mailboxes(db, ctx.user)
    cleaned = len([e for e, _ in list_auto_actions(db, ctx.user, days=1) if e.undone_at is None])
    return render(request, "dashboard.html", ctx, pending=pending, boxes=boxes, cleaned=cleaned)


@router.get("/proposals", response_class=HTMLResponse)
def proposals_list(request: Request, status: str = "pending", ctx: WebContext = Depends(web_user), db: Session = Depends(get_db)):
    status_filter = None if status == "all" else status
    items = [public_view(p) for p in visible_proposals(db, ctx.user, status=status_filter)]
    return render(request, "proposals.html", ctx, items=items, status=status)


@router.get("/proposals/{proposal_id}", response_class=HTMLResponse)
def proposal_detail(request: Request, proposal_id: str, ctx: WebContext = Depends(web_user), db: Session = Depends(get_db)):
    try:
        p = get_visible(db, ctx.user, proposal_id)
    except ProposalError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    try:
        _, access = require_mailbox(db, ctx.user, p.mailbox_id)
        can_approve = access.can_approve
    except AccessDenied:
        can_approve = False
    view = public_view(p)
    return render(request, "proposal.html", ctx, p=view, external=_external(view), can_approve=can_approve)


@router.post("/proposals/{proposal_id}/approve")
def proposal_approve(request: Request, proposal_id: str, csrf: str = Form(""), code: str = Form(""), ctx: WebContext = Depends(web_user), db: Session = Depends(get_db)):
    check_csrf(ctx, csrf)
    try:
        approve(db, proposal_id=proposal_id, user=ctx.user, via="web", code=code or None, web_second_factor_ok=ctx.session.second_factor_ok)
    except ProposalError as exc:
        db.commit()  # Protokolleinträge (z. B. falscher Code) behalten
        view = public_view(get_visible(db, ctx.user, proposal_id))
        return render(request, "proposal.html", ctx, p=view, external=_external(view), can_approve=True, error=str(exc), status=exc.status)
    return redirect(f"/proposals/{proposal_id}")


@router.post("/proposals/{proposal_id}/reject")
def proposal_reject(request: Request, proposal_id: str, csrf: str = Form(""), ctx: WebContext = Depends(web_user), db: Session = Depends(get_db)):
    check_csrf(ctx, csrf)
    try:
        reject(db, proposal_id=proposal_id, user=ctx.user, via="web")
    except ProposalError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return redirect(f"/proposals/{proposal_id}")


# ================================================================== Postfächer

@router.get("/mailboxes", response_class=HTMLResponse)
def mailboxes(request: Request, ctx: WebContext = Depends(web_user), db: Session = Depends(get_db)):
    settings = get_settings()
    return render(
        request, "mailboxes.html", ctx, boxes=accessible_mailboxes(db, ctx.user),
        google_ready=bool(settings.google_client_id and settings.google_client_secret),
        redirect_uri=settings.google_redirect_uri, info=request.query_params.get("info"), error=request.query_params.get("fehler"),
    )


@router.post("/mailboxes/google/start")
def google_start(request: Request, csrf: str = Form(""), ctx: WebContext = Depends(web_user), db: Session = Depends(get_db)):
    from ..providers.gmail import build_auth_url, new_pkce_pair

    check_csrf(ctx, csrf)
    settings = get_settings()
    if not settings.google_client_id:
        raise HTTPException(400, "Google ist noch nicht eingerichtet (LOKYY_GOOGLE_CLIENT_ID fehlt).")
    verifier, challenge = new_pkce_pair()
    state = new_token(32)
    # Alte, nicht abgeschlossene Versuche aufräumen
    for old in db.execute(select(OAuthState).where(OAuthState.created_at < utcnow() - timedelta(minutes=15))).scalars():
        db.delete(old)
    db.add(OAuthState(state=state, user_id=ctx.user.id, code_verifier=verifier))
    url = build_auth_url(client_id=settings.google_client_id, redirect_uri=settings.google_redirect_uri, state=state, code_challenge=challenge)
    return redirect(url)


@router.get("/mailboxes/google/callback")
def google_callback(request: Request, state: str = "", code: str = "", error: str = "", ctx: WebContext = Depends(web_user), db: Session = Depends(get_db)):
    from ..providers.gmail import exchange_code, fetch_profile_address

    record = db.get(OAuthState, state) if state else None
    if record is None or record.user_id != ctx.user.id or record.created_at < utcnow() - timedelta(minutes=15):
        return redirect("/mailboxes?fehler=Die+Verbindung+ist+abgelaufen.+Bitte+erneut+starten.")
    verifier = record.code_verifier
    db.delete(record)
    if error or not code:
        return redirect("/mailboxes?fehler=Google-Anmeldung+abgebrochen.")
    settings = get_settings()
    try:
        tokens = exchange_code(
            client_id=settings.google_client_id, client_secret=settings.google_client_secret,
            redirect_uri=settings.google_redirect_uri, code=code, code_verifier=verifier,
        )
        address = fetch_profile_address(tokens["access_token"])
    except ProviderError as exc:
        return redirect("/mailboxes?fehler=" + str(exc).replace(" ", "+"))

    mailbox = db.execute(select(Mailbox).where(Mailbox.provider == "gmail", Mailbox.address == address)).scalar_one_or_none()
    if mailbox is None:
        mailbox = Mailbox(provider="gmail", address=address, display_name=address, ai_enabled=False)
        db.add(mailbox)
        db.flush()
    else:
        existing = db.execute(select(MailboxAccess).where(MailboxAccess.mailbox_id == mailbox.id)).scalars().all()
        if existing and not any(a.user_id == ctx.user.id for a in existing):
            return redirect("/mailboxes?fehler=Dieses+Postfach+gehört+bereits+jemand+anderem.")
    mailbox.credentials_enc = encrypt_json({"refresh_token": tokens["refresh_token"]}, context=credentials_context(mailbox.id))
    mailbox.status = "active"
    if not db.execute(select(MailboxAccess).where(MailboxAccess.mailbox_id == mailbox.id, MailboxAccess.user_id == ctx.user.id)).scalar_one_or_none():
        db.add(MailboxAccess(user_id=ctx.user.id, mailbox_id=mailbox.id, role="owner"))
    audit.log(db, "mailbox.connected", actor_type="user", actor_id=ctx.user.id, mailbox_id=mailbox.id, provider="gmail")
    return redirect("/mailboxes?info=Postfach+verbunden.+KI-Zugriff+ist+noch+aus.")


@router.post("/mailboxes/demo")
def demo_mailbox(request: Request, csrf: str = Form(""), ctx: WebContext = Depends(web_user), db: Session = Depends(get_db)):
    check_csrf(ctx, csrf)
    if not get_settings().demo_mode:
        raise HTTPException(404)
    address = f"demo-{ctx.user.id[:6]}@lokyy-demo.de"
    mailbox = Mailbox(provider="demo", address=address, display_name="Demo-Postfach", ai_enabled=True)
    db.add(mailbox)
    db.flush()
    db.add(MailboxAccess(user_id=ctx.user.id, mailbox_id=mailbox.id, role="owner"))
    audit.log(db, "mailbox.connected", actor_type="user", actor_id=ctx.user.id, mailbox_id=mailbox.id, provider="demo")
    return redirect("/mailboxes?info=Demo-Postfach+angelegt.")


@router.post("/mailboxes/{mailbox_id}/ai")
def toggle_ai(request: Request, mailbox_id: str, enabled: str = Form("0"), csrf: str = Form(""), ctx: WebContext = Depends(web_user), db: Session = Depends(get_db)):
    check_csrf(ctx, csrf)
    try:
        mailbox, access = require_mailbox(db, ctx.user, mailbox_id)
    except AccessDenied as exc:
        raise HTTPException(403, str(exc)) from exc
    if access.role != "owner" and not ctx.user.is_admin:
        raise HTTPException(403, "Nur Inhaber dürfen den KI-Zugriff ändern.")
    mailbox.ai_enabled = enabled == "1"
    audit.log(db, "mailbox.ai_enabled" if mailbox.ai_enabled else "mailbox.ai_disabled", actor_type="user", actor_id=ctx.user.id, mailbox_id=mailbox.id)
    return redirect("/mailboxes")


@router.post("/mailboxes/{mailbox_id}/cleanup")
def toggle_cleanup(request: Request, mailbox_id: str, enabled: str = Form("0"), csrf: str = Form(""), ctx: WebContext = Depends(web_user), db: Session = Depends(get_db)):
    """'Aufräumen automatisch': Die KI darf Rückgängig-machbares ohne Freigabe tun (Stundenlimit, Bericht, Rückgängig)."""
    check_csrf(ctx, csrf)
    try:
        mailbox, access = require_mailbox(db, ctx.user, mailbox_id)
    except AccessDenied as exc:
        raise HTTPException(403, str(exc)) from exc
    if access.role != "owner" and not ctx.user.is_admin:
        raise HTTPException(403, "Nur Inhaber dürfen das ändern.")
    mailbox.auto_cleanup = enabled == "1"
    audit.log(db, "mailbox.cleanup_enabled" if mailbox.auto_cleanup else "mailbox.cleanup_disabled", actor_type="user", actor_id=ctx.user.id, mailbox_id=mailbox.id)
    return redirect("/mailboxes?info=" + ("Aufräumen+läuft+jetzt+automatisch.+Alles+steht+unter+%E2%80%9EAufgeräumt%E2%80%9C+und+lässt+sich+zurückholen." if mailbox.auto_cleanup else "Aufräumen+braucht+wieder+Freigabe."))


@router.get("/cleanup", response_class=HTMLResponse)
def cleanup_report(request: Request, ctx: WebContext = Depends(web_user), db: Session = Depends(get_db)):
    days = get_settings().undo_days
    return render(request, "cleanup.html", ctx, items=list_auto_actions(db, ctx.user, days=days), days=days,
                  info=request.query_params.get("info"), error=request.query_params.get("fehler"))


@router.post("/cleanup/{entry_id}/undo")
def cleanup_undo(request: Request, entry_id: int, csrf: str = Form(""), ctx: WebContext = Depends(web_user), db: Session = Depends(get_db)):
    check_csrf(ctx, csrf)
    try:
        undo_auto(db, entry_id=entry_id, user=ctx.user)
    except ProposalError as exc:
        db.commit()
        from urllib.parse import quote

        return redirect("/cleanup?fehler=" + quote(str(exc)))
    return redirect("/cleanup?info=Zurückgeholt.")


@router.post("/mailboxes/{mailbox_id}/sending")
def toggle_sending(request: Request, mailbox_id: str, enabled: str = Form("0"), code: str = Form(""), csrf: str = Form(""), ctx: WebContext = Depends(web_user), db: Session = Depends(get_db)):
    """'Senden komplett aus' umschalten. Wieder einschalten braucht einen frischen Zwei-Faktor-Code."""
    check_csrf(ctx, csrf)
    try:
        mailbox, access = require_mailbox(db, ctx.user, mailbox_id)
    except AccessDenied as exc:
        raise HTTPException(403, str(exc)) from exc
    if access.role != "owner" and not ctx.user.is_admin:
        raise HTTPException(403, "Nur Inhaber dürfen das ändern.")
    if enabled == "1":
        secret = decrypt(ctx.user.totp_secret_enc or "", context=f"totp:{ctx.user.id}") if ctx.user.totp_secret_enc else ""
        step = verify_totp(secret, code, ctx.user.totp_last_step) if secret else None
        if step is None:
            return redirect("/mailboxes?fehler=Zum+Einschalten+von+Senden+bitte+einen+gültigen+Code+eingeben.")
        ctx.user.totp_last_step = step
        mailbox.send_disabled = False
        audit.log(db, "mailbox.sending_enabled", actor_type="user", actor_id=ctx.user.id, mailbox_id=mailbox.id)
        return redirect("/mailboxes?info=Senden+ist+wieder+möglich+(immer+mit+Freigabe).")
    mailbox.send_disabled = True
    # Offene Sende-Anträge sofort ungültig machen
    from ..models import Proposal
    from ..proposals import SEND_ACTIONS, _scrub

    for p in db.execute(select(Proposal).where(Proposal.mailbox_id == mailbox.id, Proposal.status == "pending", Proposal.action.in_(SEND_ACTIONS))).scalars():
        p.status = "rejected"
        p.error = "Senden wurde für dieses Postfach abgeschaltet."
        _scrub(p)
    audit.log(db, "mailbox.sending_disabled", actor_type="user", actor_id=ctx.user.id, mailbox_id=mailbox.id)
    return redirect("/mailboxes?info=Senden+ist+jetzt+komplett+aus.")


@router.post("/mailboxes/{mailbox_id}/disconnect")
def disconnect(request: Request, mailbox_id: str, csrf: str = Form(""), ctx: WebContext = Depends(web_user), db: Session = Depends(get_db)):
    check_csrf(ctx, csrf)
    try:
        mailbox, access = require_mailbox(db, ctx.user, mailbox_id)
    except AccessDenied as exc:
        raise HTTPException(403, str(exc)) from exc
    if access.role != "owner" and not ctx.user.is_admin:
        raise HTTPException(403, "Nur Inhaber dürfen trennen.")
    mailbox.credentials_enc = None
    mailbox.status = "disconnected"
    mailbox.ai_enabled = False
    audit.log(db, "mailbox.disconnected", actor_type="user", actor_id=ctx.user.id, mailbox_id=mailbox.id)
    return redirect("/mailboxes?info=Postfach+getrennt.+Zugangsdaten+gelöscht.")


@router.get("/mailboxes/{mailbox_id}", response_class=HTMLResponse)
def inbox(request: Request, mailbox_id: str, q: str = "", info: str = "", fehler: str = "", ctx: WebContext = Depends(web_user), db: Session = Depends(get_db)):
    try:
        mailbox, _ = require_mailbox(db, ctx.user, mailbox_id)
        result = search(db, ctx.user, mailbox_id, q or "in:inbox", 25, None, for_ai=False, actor=ctx.user.id)
    except AccessDenied as exc:
        raise HTTPException(403, str(exc)) from exc
    except MailError as exc:
        return render(request, "inbox.html", ctx, mailbox=mailbox if 'mailbox' in locals() else None, messages=[], q=q, error=str(exc), status=exc.status)
    return render(request, "inbox.html", ctx, mailbox=mailbox, messages=result["messages"], q=q, info=info, error=fehler or None)


@router.get("/mailboxes/{mailbox_id}/messages/{message_id}", response_class=HTMLResponse)
def message_view(request: Request, mailbox_id: str, message_id: str, ctx: WebContext = Depends(web_user), db: Session = Depends(get_db)):
    try:
        mailbox, access = require_mailbox(db, ctx.user, mailbox_id)
        msg = message_for_human(db, ctx.user, mailbox_id, message_id)
    except AccessDenied as exc:
        raise HTTPException(403, str(exc)) from exc
    except MailError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return render(request, "message.html", ctx, mailbox=mailbox, msg=msg, can_approve=access.can_approve)


@router.post("/mailboxes/{mailbox_id}/messages/{message_id}/propose")
def propose_from_web(
    request: Request, mailbox_id: str, message_id: str, action: str = Form(...), body: str = Form(""),
    reply_all: str = Form("0"), to: str = Form(""), csrf: str = Form(""),
    ctx: WebContext = Depends(web_user), db: Session = Depends(get_db),
):
    """Ein Mensch stellt selbst einen Antrag (z. B. Antwort schreiben) und gibt ihn danach frei."""
    check_csrf(ctx, csrf)
    params: dict[str, Any] = {"message_id": message_id}
    if action == "reply":
        params.update(body=body, reply_all=reply_all == "1")
    elif action == "forward":
        params.update(to=[t for t in to.split(",") if t.strip()], body=body)
    elif action not in ("archive", "trash", "mark_read", "mark_unread", "spam", "untrash"):
        raise HTTPException(400, "Unbekannte Aktion.")
    try:
        p = create_proposal(db, user=ctx.user, mailbox_id=mailbox_id, action=action, params=params, via="web")
    except ProposalError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    if p.status != "pending":  # Aufräumen läuft sofort: dein Klick war die Freigabe
        from urllib.parse import quote

        if p.status == "executed":
            return redirect(f"/mailboxes/{mailbox_id}?info={quote('Erledigt.')}")
        return redirect(f"/mailboxes/{mailbox_id}?fehler={quote(p.error or 'Nicht ausgeführt.')}")
    return redirect(f"/proposals/{p.id}")


# ================================================================== Schlüssel

@router.get("/keys", response_class=HTMLResponse)
def keys(request: Request, ctx: WebContext = Depends(web_user), db: Session = Depends(get_db)):
    items = db.execute(select(ApiKey).where(ApiKey.user_id == ctx.user.id).order_by(ApiKey.created_at.desc())).scalars().all()
    return render(request, "keys.html", ctx, items=items, public_url=get_settings().public_url.rstrip("/"))


@router.post("/keys", response_class=HTMLResponse)
def key_create(request: Request, kind: str = Form(...), name: str = Form(...), csrf: str = Form(""), ctx: WebContext = Depends(web_user), db: Session = Depends(get_db)):
    check_csrf(ctx, csrf)
    if kind not in ("ai", "device"):
        raise HTTPException(400, "Unbekannte Schlüsselart.")
    name = name.strip()[:100] or ("KI-Zugang" if kind == "ai" else "Hermes Desktop")
    plaintext, prefix, digest = new_api_key(kind)
    key = ApiKey(user_id=ctx.user.id, kind=kind, name=name, prefix=prefix, secret_hash=digest)
    db.add(key)
    db.flush()
    audit.log(db, "key.created", actor_type="user", actor_id=ctx.user.id, key_kind=kind, key_name=name)
    return render(request, "key_created.html", ctx, plaintext=plaintext, key=key, public_url=get_settings().public_url.rstrip("/"))


@router.post("/keys/{key_id}/revoke")
def key_revoke(request: Request, key_id: str, csrf: str = Form(""), ctx: WebContext = Depends(web_user), db: Session = Depends(get_db)):
    check_csrf(ctx, csrf)
    key = db.get(ApiKey, key_id)
    if key is None or (key.user_id != ctx.user.id and not ctx.user.is_admin):
        raise HTTPException(404)
    key.revoked = True
    audit.log(db, "key.revoked", actor_type="user", actor_id=ctx.user.id, key_kind=key.kind, key_name=key.name)
    return redirect("/keys")


# ================================================================== Konto

@router.get("/account", response_class=HTMLResponse)
def account(request: Request, ctx: WebContext = Depends(web_user)):
    return render(request, "account.html", ctx, info=request.query_params.get("info"))


@router.post("/account/telegram/start", response_class=HTMLResponse)
def telegram_start(request: Request, csrf: str = Form(""), ctx: WebContext = Depends(web_user), db: Session = Depends(get_db)):
    check_csrf(ctx, csrf)
    api = tg.get_api()
    if api is None:
        raise HTTPException(400, "Telegram ist nicht eingerichtet (LOKYY_TELEGRAM_BOT_TOKEN fehlt).")
    username = ""
    try:
        username = (api.call("getMe") or {}).get("username", "")
    except tg.TelegramError:
        pass
    code = tg.create_pairing(db, ctx.user)
    return render(request, "account.html", ctx, pairing_code=code, bot_username=username)


@router.post("/account/telegram/unlink")
def telegram_unlink(request: Request, csrf: str = Form(""), ctx: WebContext = Depends(web_user), db: Session = Depends(get_db)):
    check_csrf(ctx, csrf)
    tg.unlink(db, ctx.user)
    return redirect("/account?info=Telegram+getrennt.")


@router.post("/account/password", response_class=HTMLResponse)
def account_password(request: Request, current: str = Form(...), new: str = Form(...), csrf: str = Form(""), ctx: WebContext = Depends(web_user), db: Session = Depends(get_db)):
    check_csrf(ctx, csrf)
    if not verify_password(ctx.user.password_hash, current):
        return render(request, "account.html", ctx, error="Das aktuelle Passwort stimmt nicht.", status=400)
    problems = password_problems(new)
    if problems:
        return render(request, "account.html", ctx, error=" ".join(problems), status=400)
    ctx.user.password_hash = hash_password(new)
    # Alle anderen Sitzungen beenden
    for s in db.execute(select(WebSession).where(WebSession.user_id == ctx.user.id, WebSession.id != ctx.session.id)).scalars():
        db.delete(s)
    audit.log(db, "auth.password_changed", actor_type="user", actor_id=ctx.user.id)
    return redirect("/account?info=Passwort+geändert.")


# ================================================================== Verwaltung (Admin)

@router.get("/users", response_class=HTMLResponse)
def users(request: Request, ctx: WebContext = Depends(web_admin), db: Session = Depends(get_db)):
    items = db.execute(select(User).order_by(User.email)).scalars().all()
    return render(request, "users.html", ctx, items=items, info=request.query_params.get("info"))


@router.post("/users", response_class=HTMLResponse)
def user_create(
    request: Request, email: str = Form(...), display_name: str = Form(""), role: str = Form("member"),
    password: str = Form(...), csrf: str = Form(""), ctx: WebContext = Depends(web_admin), db: Session = Depends(get_db),
):
    check_csrf(ctx, csrf)
    email = email.strip().lower()
    items = db.execute(select(User).order_by(User.email)).scalars().all()
    if "@" not in email or db.execute(select(User).where(User.email == email)).scalar_one_or_none():
        return render(request, "users.html", ctx, items=items, error="Ungültige oder bereits vergebene E-Mail.", status=400)
    problems = password_problems(password)
    if problems:
        return render(request, "users.html", ctx, items=items, error="Startpasswort: " + " ".join(problems), status=400)
    user = User(email=email, display_name=display_name.strip()[:200], role="admin" if role == "admin" else "member", password_hash=hash_password(password))
    db.add(user)
    db.flush()
    audit.log(db, "user.created", actor_type="user", actor_id=ctx.user.id, role=user.role)
    return redirect("/users?info=Nutzer+angelegt.+Beim+ersten+Login+richtet+er+Zwei-Faktor+ein.")


@router.post("/users/{user_id}/disable")
def user_disable(request: Request, user_id: str, csrf: str = Form(""), ctx: WebContext = Depends(web_admin), db: Session = Depends(get_db)):
    check_csrf(ctx, csrf)
    user = db.get(User, user_id)
    if user is None or user.id == ctx.user.id:
        raise HTTPException(400, "Nicht möglich.")
    user.disabled = True
    for s in db.execute(select(WebSession).where(WebSession.user_id == user.id)).scalars():
        db.delete(s)
    audit.log(db, "user.disabled", actor_type="user", actor_id=ctx.user.id)
    return redirect("/users?info=Nutzer+gesperrt.")


@router.get("/audit", response_class=HTMLResponse)
def audit_view(request: Request, ctx: WebContext = Depends(web_admin), db: Session = Depends(get_db)):
    items = db.execute(select(AuditEvent).order_by(AuditEvent.ts.desc()).limit(300)).scalars().all()
    return render(request, "audit.html", ctx, items=items)


@router.get("/audit.csv")
def audit_csv(ctx: WebContext = Depends(web_admin), db: Session = Depends(get_db)) -> Response:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["zeit_utc", "akteur_typ", "akteur_id", "ereignis", "postfach_id", "antrag_id", "details"])
    for e in db.execute(select(AuditEvent).order_by(AuditEvent.ts)).scalars():
        writer.writerow([e.ts.isoformat(), e.actor_type, e.actor_id or "", e.event, e.mailbox_id or "", e.proposal_id or "", e.detail])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=lokyymail-protokoll.csv"})
