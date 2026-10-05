"""LokyyMail – Backend-Teil des Hermes-Plugins (läuft im Hermes-Gateway).

Reicht Anfragen der Desktop-Oberfläche an die LokyyMail-Geräte-Schnittstelle weiter.
Sicherheit:
- Der Geräte-Schlüssel kann lesen, Anträge stellen und ablehnen. Freigeben geht nur mit einem
  frischen Zwei-Faktor-Code, den der Mensch aus seiner Authenticator-App eintippt.
- Senden und riskante Aktionen lassen sich hier gar nicht freigeben – nur auf der Freigabe-Webseite.
  Grund: Hermes läuft in einer Umgebung, die der Agent selbst kontrolliert.
- Der Schlüssel wird nie an die Oberfläche zurückgegeben.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()

_ID = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_TIMEOUT = 25


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home()).expanduser()
    except Exception:
        return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def _config_path() -> Path:
    return _home() / "lokyymail.json"


def _config() -> tuple[str, str]:
    url = os.environ.get("LOKYYMAIL_URL", "").strip()
    token = os.environ.get("LOKYYMAIL_DEVICE_TOKEN", "").strip()
    if not (url and token):
        try:
            data = json.loads(_config_path().read_text())
            url, token = data.get("url", ""), data.get("token", "")
        except (OSError, ValueError):
            pass
    return url.rstrip("/"), token


def _valid_url(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" and parts.netloc:
        return True
    return parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1")


def _call(method: str, path: str, body: Any = None, *, url: str | None = None, token: str | None = None) -> Any:
    base, key = (url, token) if url else _config()
    if not base or not key:
        raise HTTPException(status_code=409, detail="LokyyMail ist noch nicht eingerichtet.")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + "/api/device" + path, data=data, method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 (URL ist geprüft)
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read() or b"{}").get("detail", "Fehler bei LokyyMail.")
        except ValueError:
            detail = "Fehler bei LokyyMail."
        raise HTTPException(status_code=exc.code, detail=detail) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise HTTPException(status_code=502, detail="LokyyMail ist nicht erreichbar.") from None


def _id(value: str) -> str:
    if not _ID.fullmatch(value or ""):
        raise HTTPException(status_code=422, detail="Ungültige ID.")
    return value


# ------------------------------------------------------------------ Einrichtung

@router.get("/config")
def get_config() -> dict[str, Any]:
    url, token = _config()
    return {"configured": bool(url and token), "url": url}


@router.post("/config")
async def set_config(request: Request) -> dict[str, Any]:
    body = await request.json()
    url = str(body.get("url", "")).strip().rstrip("/")
    token = str(body.get("token", "")).strip()
    if not _valid_url(url):
        raise HTTPException(status_code=422, detail="Bitte eine https-Adresse angeben (z. B. https://mail.firma.de).")
    if not token.startswith("lkdv_"):
        raise HTTPException(status_code=422, detail="Das ist kein Hermes-Desktop-Zugang (beginnt mit lkdv_).")
    _call("GET", "/status", url=url, token=token)  # Verbindung testen
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": url, "token": token}))
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return {"configured": True, "url": url}


# ------------------------------------------------------------------ Weiterleitung

@router.get("/status")
def status() -> Any:
    return _call("GET", "/status")


@router.get("/mailboxes/{mailbox_id}/messages")
def messages(mailbox_id: str, q: str = "", page_token: str = "") -> Any:
    query = urllib.parse.urlencode({k: v for k, v in {"q": q[:500], "page_token": page_token[:2048]}.items() if v})
    return _call("GET", f"/mailboxes/{_id(mailbox_id)}/messages" + (f"?{query}" if query else ""))


@router.get("/mailboxes/{mailbox_id}/messages/{message_id}")
def message(mailbox_id: str, message_id: str) -> Any:
    return _call("GET", f"/mailboxes/{_id(mailbox_id)}/messages/{_id(message_id)}")


@router.post("/mailboxes/{mailbox_id}/proposals")
async def propose(mailbox_id: str, request: Request) -> Any:
    body = await request.json()
    return _call("POST", f"/mailboxes/{_id(mailbox_id)}/proposals", {
        "action": str(body.get("action", ""))[:32],
        "params": body.get("params") if isinstance(body.get("params"), dict) else {},
        "note": str(body.get("note", ""))[:500],
    })


@router.get("/proposals")
def proposals(status: str = "pending") -> Any:
    if status not in ("pending", "executed", "rejected", "all"):
        status = "pending"
    return _call("GET", f"/proposals?status={status}")


@router.post("/proposals/{proposal_id}/approve")
async def approve(proposal_id: str, request: Request) -> Any:
    body = await request.json()
    code = re.sub(r"\D", "", str(body.get("code", "")))[:8]
    return _call("POST", f"/proposals/{_id(proposal_id)}/approve", {"code": code})


@router.post("/proposals/{proposal_id}/reject")
def reject(proposal_id: str) -> Any:
    return _call("POST", f"/proposals/{_id(proposal_id)}/reject", {})
