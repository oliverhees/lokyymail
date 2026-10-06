"""Gmail-Anschluss für Google Workspace (eigenes Google-Cloud-Projekt des Kunden, Typ "Intern").

- Anmeldung pro Mitarbeiter über OAuth mit PKCE.
- Der Refresh-Token liegt verschlüsselt in der Datenbank, nie im Klartext.
- Feste Zeitlimits, keine automatischen Wiederholungen bei schreibenden Aufrufen.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from email.utils import getaddresses
from typing import Any, Callable
from urllib.parse import urlencode

import requests

from .base import MailProvider, MessageDetail, MessageSummary, OutgoingMail, ProviderError, build_mime

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
TIMEOUT = 15
MAX_FORWARD_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024


# ------------------------------------------------------------------ OAuth

def new_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_auth_url(*, client_id: str, redirect_uri: str, state: str, code_challenge: str, login_hint: str = "") -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return AUTH_URL + "?" + urlencode(params)


def exchange_code(*, client_id: str, client_secret: str, redirect_uri: str, code: str, code_verifier: str) -> dict[str, Any]:
    try:
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": code_verifier,
            },
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise ProviderError("Google ist gerade nicht erreichbar.") from exc
    if resp.status_code != 200:
        raise ProviderError("Google hat die Anmeldung abgelehnt. Bitte erneut verbinden.")
    data = resp.json()
    if not data.get("refresh_token"):
        raise ProviderError("Google hat keinen dauerhaften Zugang erteilt. Bitte die Verbindung neu starten.")
    granted = set((data.get("scope") or "").split())
    if not set(SCOPES) <= granted:
        raise ProviderError("Es wurden nicht alle nötigen Gmail-Rechte erteilt.")
    return data


def _profile_error(resp: requests.Response) -> str:
    """Nennt Googles Grund (z. B. Gmail API nicht aktiviert), damit man nicht raten muss."""
    reason = ""
    try:
        err = resp.json().get("error") or {}
        reason = str(err.get("message") or err.get("status") or "")[:160]
    except ValueError:
        pass
    hint = " Ist die Gmail API im Google-Cloud-Projekt aktiviert?" if resp.status_code == 403 else ""
    return f"Gmail-Profil konnte nicht gelesen werden (Google: HTTP {resp.status_code}{', ' + reason if reason else ''}).{hint}"


def fetch_profile_address(access_token: str) -> str:
    try:
        resp = requests.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/profile",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise ProviderError("Gmail ist gerade nicht erreichbar.") from exc
    if resp.status_code != 200:
        raise ProviderError(_profile_error(resp))
    address = (resp.json().get("emailAddress") or "").strip().lower()
    if "@" not in address:
        raise ProviderError("Gmail hat keine gültige Adresse geliefert.")
    return address


# ------------------------------------------------------------------ Hilfen

def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in payload.get("headers", []) or []:
        name = str(h.get("name", "")).lower()
        if name and name not in out:
            out[name] = str(h.get("value", ""))[:4000]
    return out


def _walk(part: dict[str, Any], plain: list[str], html: list[str], attachments: list[dict[str, Any]]) -> None:
    mime = (part.get("mimeType") or "").lower()
    body = part.get("body") or {}
    filename = part.get("filename") or ""
    if filename:
        attachments.append({
            "part_id": str(part.get("partId") or ""), "name": filename[:255], "type": mime,
            "size": int(body.get("size") or 0), "_attachment_id": body.get("attachmentId"),
        })
        return
    if mime.startswith("multipart/"):
        for child in part.get("parts", []) or []:
            _walk(child, plain, html, attachments)
        return
    data = body.get("data")
    if not data:
        return
    try:
        text = _b64decode(data).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return
    if mime == "text/plain":
        plain.append(text)
    elif mime == "text/html":
        html.append(text)


def _summary(raw: dict[str, Any]) -> MessageSummary:
    payload = raw.get("payload") or {}
    h = _headers(payload)
    labels = list(raw.get("labelIds") or [])
    return MessageSummary(
        id=str(raw.get("id")),
        thread_id=str(raw.get("threadId")),
        from_=h.get("from", ""),
        to=h.get("to", ""),
        subject=h.get("subject", ""),
        date=h.get("date", ""),
        snippet=str(raw.get("snippet") or "")[:300],
        labels=labels,
        unread="UNREAD" in labels,
        has_attachments=any((p.get("filename") for p in (payload.get("parts") or []))),
    )


def _detail(raw: dict[str, Any]) -> MessageDetail:
    s = _summary(raw)
    payload = raw.get("payload") or {}
    h = _headers(payload)
    plain: list[str] = []
    html: list[str] = []
    attachments: list[dict[str, Any]] = []
    _walk(payload, plain, html, attachments)
    attachments = [{k: v for k, v in a.items() if not k.startswith("_")} for a in attachments]
    base = {k: getattr(s, k) for k in MessageSummary.__dataclass_fields__}
    base["has_attachments"] = s.has_attachments or bool(attachments)
    return MessageDetail(
        **base,
        cc=h.get("cc", ""),
        reply_to=h.get("reply-to", ""),
        message_id_header=h.get("message-id", ""),
        references=h.get("references", ""),
        body_plain="\n".join(plain)[:200_000],
        body_html="\n".join(html)[:400_000],
        attachments=attachments,
    )


# ------------------------------------------------------------------ Anschluss

class GmailProvider(MailProvider):
    def __init__(
        self,
        *,
        address: str,
        credentials: dict[str, Any],
        client_id: str,
        client_secret: str,
        on_token_refresh: Callable[[dict[str, Any]], None] | None = None,
        service: Any = None,
    ) -> None:
        self.address = address
        self._service = service
        self._credentials = credentials
        self._client_id = client_id
        self._client_secret = client_secret
        self._on_refresh = on_token_refresh

    @property
    def service(self) -> Any:
        if self._service is None:
            import google_auth_httplib2
            import httplib2
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            creds = Credentials(
                token=None,
                refresh_token=self._credentials["refresh_token"],
                client_id=self._client_id,
                client_secret=self._client_secret,
                token_uri=TOKEN_URL,
                scopes=SCOPES,
            )
            try:
                creds.refresh(Request())
            except Exception as exc:  # google.auth.exceptions.RefreshError u. a.
                raise ProviderError("Der Google-Zugang ist abgelaufen oder wurde widerrufen. Bitte neu verbinden.") from exc
            http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=TIMEOUT))
            self._service = build("gmail", "v1", http=http, cache_discovery=False, static_discovery=True)
        return self._service

    def _call(self, request: Any) -> dict[str, Any]:
        try:
            result = request.execute(num_retries=0)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("Gmail hat die Anfrage nicht ausgeführt.") from exc
        if not isinstance(result, dict):
            raise ProviderError("Ungültige Antwort von Gmail.")
        return result

    def search(self, query: str, max_results: int, page_token: str | None) -> tuple[list[MessageSummary], str | None]:
        params: dict[str, Any] = {"userId": "me", "q": query or "in:inbox", "maxResults": max(1, min(max_results, 50))}
        if page_token:
            params["pageToken"] = page_token
        listing = self._call(self.service.users().messages().list(**params))
        out: list[MessageSummary] = []
        for meta in listing.get("messages", []) or []:
            raw = self._call(
                self.service.users().messages().get(
                    userId="me", id=meta["id"], format="metadata",
                    metadataHeaders=["From", "To", "Cc", "Subject", "Date"],
                )
            )
            out.append(_summary(raw))
        return out, listing.get("nextPageToken")

    def get_message(self, message_id: str) -> MessageDetail:
        return _detail(self._call(self.service.users().messages().get(userId="me", id=message_id, format="full")))

    def get_thread(self, thread_id: str) -> list[MessageDetail]:
        raw = self._call(self.service.users().threads().get(userId="me", id=thread_id, format="full"))
        items = [_detail(m) for m in raw.get("messages", []) or []]
        if not items:
            raise ProviderError("Unterhaltung nicht gefunden.")
        return items

    def get_raw(self, message_id: str) -> bytes:
        raw = self._call(self.service.users().messages().get(userId="me", id=message_id, format="raw"))
        data = _b64decode(raw.get("raw", ""))
        if len(data) > MAX_FORWARD_BYTES:
            raise ProviderError("Die Nachricht ist zu groß zum Weiterleiten (über 20 MB).")
        return data

    def list_labels(self) -> list[dict[str, str]]:
        raw = self._call(self.service.users().labels().list(userId="me"))
        return [
            {"id": str(l.get("id")), "name": str(l.get("name")), "type": str(l.get("type", "user"))}
            for l in raw.get("labels", []) or []
        ]

    def snapshot(self, message_id: str) -> dict[str, Any]:
        raw = self._call(self.service.users().messages().get(userId="me", id=message_id, format="minimal"))
        return {"id": str(raw.get("id")), "thread_id": str(raw.get("threadId")), "labels": sorted(raw.get("labelIds") or [])}

    def message_exists_in_sent(self, message_id: str) -> bool:
        try:
            snap = self.snapshot(message_id)
        except ProviderError:
            return False
        return "SENT" in snap["labels"]

    def modify_labels(self, message_id: str, add: list[str], remove: list[str]) -> None:
        self._call(
            self.service.users().messages().modify(
                userId="me", id=message_id, body={"addLabelIds": add, "removeLabelIds": remove}
            )
        )

    def trash(self, message_id: str) -> None:
        self._call(self.service.users().messages().trash(userId="me", id=message_id))

    def untrash(self, message_id: str) -> None:
        self._call(self.service.users().messages().untrash(userId="me", id=message_id))

    def get_attachment(self, message_id: str, part_id: str) -> tuple[bytes, str, str]:
        raw = self._call(self.service.users().messages().get(userId="me", id=message_id, format="full"))
        found: list[dict[str, Any]] = []
        _walk(raw.get("payload") or {}, [], [], found)
        part = next((a for a in found if a["part_id"] == str(part_id)), None)
        if part is None or not part.get("_attachment_id"):
            raise ProviderError("Anhang nicht gefunden. part_id aus read_message verwenden.")
        if part["size"] > MAX_ATTACHMENT_BYTES:
            raise ProviderError(f"Anhang zu groß ({part['size'] // 1024 // 1024} MB, Limit 15 MB).")
        data = self._call(self.service.users().messages().attachments().get(
            userId="me", messageId=message_id, id=part["_attachment_id"]))
        return _b64decode(data.get("data", "")), part["name"], part["type"]

    def send(self, mail: OutgoingMail) -> str:
        body: dict[str, Any] = {"raw": base64.urlsafe_b64encode(build_mime(self.address, mail)).decode("ascii")}
        if mail.thread_id:
            body["threadId"] = mail.thread_id
        result = self._call(self.service.users().messages().send(userId="me", body=body))
        sent_id = str(result.get("id") or "")
        if not sent_id:
            raise ProviderError("Gmail hat keine Bestätigung für den Versand geliefert.")
        return sent_id


def split_addresses(value: str) -> list[str]:
    return [addr.strip().lower() for _, addr in getaddresses([value or ""]) if addr and "@" in addr]
