"""Gemeinsame Bausteine der Web-Schicht: Sitzungen, CSRF, Schlüssel, einfache Bremse gegen Passwort-Raten."""

from __future__ import annotations

import hmac
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import ApiKey, User, WebSession, utcnow
from ..security import new_token, parse_api_key, sha256_hex

SESSION_COOKIE = "lokyy_session"
PRE_CSRF_COOKIE = "lokyy_pre"

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _local_time(value: object) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if not isinstance(value, datetime):
        return str(value)
    return value.astimezone(ZoneInfo(get_settings().timezone)).strftime("%d.%m.%Y, %H:%M")


templates.env.filters["local"] = _local_time


# ------------------------------------------------------------------ Bremse

class RateLimiter:
    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        """True = erlaubt, False = gebremst."""
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


login_limiter = RateLimiter(limit=10, window_seconds=900)
code_limiter = RateLimiter(limit=10, window_seconds=900)


def client_ip(request: Request) -> str:
    # Hinter Coolify/Traefik steht die echte IP in X-Forwarded-For (vom Proxy gesetzt).
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


# ------------------------------------------------------------------ Web-Sitzungen

@dataclass
class WebContext:
    session: WebSession
    user: User

    @property
    def csrf(self) -> str:
        return self.session.csrf_token


def create_session(db: Session, user: User) -> str:
    token = new_token(32)
    db.add(
        WebSession(
            id=sha256_hex(token),
            user_id=user.id,
            csrf_token=new_token(24),
            second_factor_ok=False,
            expires_at=utcnow() + timedelta(hours=get_settings().session_hours),
        )
    )
    return token


def load_session(request: Request, db: Session) -> WebContext | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    s = db.get(WebSession, sha256_hex(token))
    if s is None or s.expires_at < utcnow() or s.user.disabled:
        return None
    return WebContext(session=s, user=s.user)


class LoginRequired(Exception):
    def __init__(self, target: str = "/login") -> None:
        self.target = target


def web_user(request: Request, db: Session = Depends(get_db)) -> WebContext:
    ctx = load_session(request, db)
    if ctx is None:
        raise LoginRequired("/login")
    if not ctx.user.totp_enabled:
        raise LoginRequired("/setup/2fa")
    if not ctx.session.second_factor_ok:
        raise LoginRequired("/login/code")
    return ctx


def web_admin(ctx: WebContext = Depends(web_user)) -> WebContext:
    if not ctx.user.is_admin:
        raise HTTPException(status_code=403, detail="Nur für Administratoren.")
    return ctx


def check_csrf(ctx: WebContext, token: str | None) -> None:
    if not token or not hmac.compare_digest(token, ctx.csrf):
        raise HTTPException(status_code=400, detail="Formular abgelaufen. Bitte Seite neu laden.")


def check_pre_csrf(request: Request, token: str | None) -> None:
    cookie = request.cookies.get(PRE_CSRF_COOKIE)
    if not token or not cookie or not hmac.compare_digest(token, cookie):
        raise HTTPException(status_code=400, detail="Formular abgelaufen. Bitte Seite neu laden.")


def set_cookie(response, name: str, value: str, *, max_age: int | None = None, samesite: str = "strict") -> None:
    response.set_cookie(
        name, value, max_age=max_age, httponly=True, samesite=samesite,
        secure=get_settings().secure_cookies, path="/",
    )


# ------------------------------------------------------------------ Maschinen-Schlüssel

@dataclass
class KeyContext:
    key: ApiKey
    user: User


def resolve_api_key(db: Session, authorization: str | None, *, kind: str) -> KeyContext:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Schlüssel fehlt.")
    plaintext = authorization[7:].strip()
    parsed = parse_api_key(plaintext)
    if parsed is None:
        raise HTTPException(status_code=401, detail="Ungültiger Schlüssel.")
    key_kind, prefix = parsed
    if key_kind != kind:
        # Ein KI-Schlüssel darf niemals die Geräte-Schnittstelle (mit Freigaben) benutzen – und umgekehrt.
        raise HTTPException(status_code=403, detail="Dieser Schlüssel ist für diese Schnittstelle nicht zugelassen.")
    digest = sha256_hex(plaintext)
    for key in db.execute(select(ApiKey).where(ApiKey.prefix == prefix, ApiKey.kind == kind)).scalars():
        if hmac.compare_digest(key.secret_hash, digest):
            if key.revoked or key.user.disabled:
                raise HTTPException(status_code=401, detail="Schlüssel wurde widerrufen.")
            now = utcnow()
            if key.last_used_at is None or now - key.last_used_at > timedelta(minutes=5):
                key.last_used_at = now
            return KeyContext(key=key, user=key.user)
    raise HTTPException(status_code=401, detail="Ungültiger Schlüssel.")


def device_key(request: Request, db: Session = Depends(get_db)) -> KeyContext:
    return resolve_api_key(db, request.headers.get("authorization"), kind="device")
