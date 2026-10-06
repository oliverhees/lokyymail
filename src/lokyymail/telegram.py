"""Telegram: Freigaben per Handy.

Warum Telegram ein guter Weg ist: Die KI kann Telegram nicht mitlesen. Die Vorschau kommt von LokyyMail,
nicht von der KI, und ein Code, den du hier eintippst, wird für genau den Antrag geprüft, auf den du antwortest.

- Normal (z. B. interne Antwort): ein Tipp auf „Freigeben“.
- Wichtig (extern, Anhang, Weiterleitung …): Antworte auf die Nachricht mit deinem 6-stelligen Code.
- Nur verbundene Konten werden bedient, fremde Klicks werden protokolliert (ohne Inhalt).
- Eigener Bot pro Kunde, Long-Polling im Worker (keine offene Schnittstelle nötig).
"""

from __future__ import annotations

import html
import logging
import re
import secrets
import threading
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .config import get_settings
from .db import session_scope
from .models import AutoAction, Mailbox, MailboxAccess, Proposal, TelegramMessage, TelegramPairing, User, utcnow
from .proposals import ProposalError, approve, expire_due, public_view, reject
from .security import sha256_hex
from .web.deps import RateLimiter

log = logging.getLogger("lokyymail.telegram")
esc = html.escape

_limiter = RateLimiter(limit=12, window_seconds=900)
_STAMPS = {
    "executed": "✅ Ausgeführt", "rejected": "❌ Abgelehnt", "expired": "⌛ Abgelaufen", "failed": "⚠️ Fehlgeschlagen",
    "uncertain": "⚠️ Unklar, bitte im Postfach prüfen", "superseded": "↻ Überholt, nichts wurde ausgeführt", "withdrawn": "↩️ Zurückgezogen",
}
_CLEANUP_NAMES = {
    "archive": "archiviert", "mark_read": "als gelesen markiert", "mark_unread": "als ungelesen markiert",
    "labels": "mit Labels versehen", "spam": "als Spam markiert", "untrash": "wiederhergestellt",
}


class TelegramError(RuntimeError):
    pass


class TelegramAPI:
    def __init__(self, token: str) -> None:
        self._base = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, *, http_timeout: int = 15, **params: Any) -> Any:
        try:
            resp = requests.post(f"{self._base}/{method}", json=params, timeout=http_timeout)
        except requests.RequestException:
            raise TelegramError("Telegram ist nicht erreichbar.") from None  # keine URL (enthält den Token) ins Log
        try:
            data = resp.json()
        except ValueError:
            raise TelegramError(f"Ungültige Antwort von Telegram ({resp.status_code}).") from None
        if not data.get("ok"):
            raise TelegramError(str(data.get("description", "Fehler"))[:200])
        return data.get("result")


_api_override: Any = None
_api_cache: TelegramAPI | None = None


def get_api() -> Any:
    global _api_cache
    if _api_override is not None:
        return _api_override
    token = get_settings().telegram_bot_token
    if not token:
        return None
    if _api_cache is None:
        _api_cache = TelegramAPI(token)
    return _api_cache


# ------------------------------------------------------------------ Darstellung

def _trunc(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…"


def render_proposal(view: dict[str, Any], *, base_url: str) -> tuple[str, dict[str, Any]]:
    """Vorschau-Text (HTML, alles Fremde escaped) und Knöpfe. Hohes Risiko: kein Freigabe-Knopf, nur Code."""
    pv = view["preview"]
    high = view["risk_level"] == "high"
    origin = {"ai": "von der KI", "device": "über Hermes"}.get(view["requested_via"], "manuell")
    head = f"✉️ <b>{esc(view['action_label'])}</b>"
    if pv.get("to"):
        head += " an " + esc(_trunc(", ".join(pv["to"]), 300))
    lines = [head, f"<i>{esc(pv.get('mailbox', ''))} · {origin}</i>"]
    if view["risk_reasons"]:
        lines.append("🔴 <b>Hohes Risiko:</b> " + esc("; ".join(view["risk_reasons"])))
    if pv.get("subject"):
        lines.append("<b>Betreff:</b> " + esc(_trunc(pv["subject"], 200)))
    if pv.get("cc"):
        lines.append("<b>Cc:</b> " + esc(_trunc(", ".join(pv["cc"]), 300)))
    original = pv.get("original")
    if original and not pv.get("body"):
        lines.append(f"<b>Mail:</b> {esc(_trunc(original.get('subject', ''), 120))} <i>von {esc(_trunc(original.get('from', ''), 120))}</i>")
    if pv.get("add"):
        lines.append("<b>Label dazu:</b> " + esc(", ".join(pv["add"])))
    if pv.get("remove"):
        lines.append("<b>Label weg:</b> " + esc(", ".join(pv["remove"])))
    if pv.get("attachments"):
        lines.append("<b>Anhänge:</b> " + esc(_trunc(", ".join(a.get("name", "") for a in pv["attachments"]), 300)))
    if pv.get("messages"):
        items = [f"• {esc(_trunc(m.get('subject') or '(ohne Betreff)', 70))}" for m in pv["messages"][:8]]
        more = len(pv["messages"]) - 8
        lines.append(f"<b>{esc(pv.get('operation', 'Sammelaktion'))}</b> für {len(pv['messages'])} Mails:\n" + "\n".join(items) + (f"\n… und {more} weitere" if more > 0 else ""))
    if pv.get("body"):
        lines.append("<pre>" + esc(_trunc(pv["body"], 1200)) + "</pre>")
    if view.get("note"):
        lines.append("<i>Begründung der KI (nicht überprüft): " + esc(_trunc(view["note"], 300)) + "</i>")
    lines.append("⏳ Läuft ab " + esc(_local_time(view["expires_at"])))
    if high:
        lines.append("👉 Antworte auf <b>diese Nachricht</b> mit deinem <b>6-stelligen Code</b> aus der Authenticator-App.")
    pid = view["id"]
    buttons = [[{"text": "❌ Ablehnen", "callback_data": f"r:{pid}"}]] if high else [[
        {"text": "✅ Freigeben", "callback_data": f"a:{pid}"}, {"text": "❌ Ablehnen", "callback_data": f"r:{pid}"}]]
    if base_url.startswith("https://"):  # Telegram erlaubt nur https-Links in Knöpfen
        buttons.append([{"text": "🔎 Auf der Webseite ansehen", "url": f"{base_url}/proposals/{pid}"}])
    return _trunc("\n".join(lines), 3900), {"inline_keyboard": buttons}


def _local_time(iso: str) -> str:
    from datetime import datetime

    try:
        return datetime.fromisoformat(iso).astimezone(ZoneInfo(get_settings().timezone)).strftime("%H:%M")
    except (ValueError, TypeError):
        return ""


# ------------------------------------------------------------------ Benachrichtigen und aufräumen

def _approvers(db: Session, mailbox_id: str) -> list[User]:
    return list(db.execute(
        select(User).join(MailboxAccess, MailboxAccess.user_id == User.id).where(
            MailboxAccess.mailbox_id == mailbox_id, MailboxAccess.role.in_(("owner", "approver")),
            User.disabled.is_(False), User.telegram_chat_id.is_not(None),
        )
    ).scalars().all())


def notify_pending(db: Session, api: Any) -> int:
    """Schickt neue offene Anträge an alle Freigeber mit verbundenem Telegram. Jeder Antrag genau einmal."""
    base = get_settings().public_url.rstrip("/")
    expire_due(db)
    sent = 0
    for p in db.execute(select(Proposal).where(Proposal.status == "pending", Proposal.notified_at.is_(None))).scalars().all():
        p.notified_at = utcnow()
        if p.requested_via == "web":  # Mensch sitzt gerade selbst davor
            continue
        text, markup = render_proposal(public_view(p), base_url=base)
        for user in _approvers(db, p.mailbox_id):
            try:
                res = api.call("sendMessage", chat_id=int(user.telegram_chat_id), text=text, parse_mode="HTML",
                               reply_markup=markup, disable_web_page_preview=True)
            except TelegramError as exc:
                log.warning("Telegram: Senden fehlgeschlagen: %s", exc)
                continue
            db.add(TelegramMessage(chat_id=user.telegram_chat_id, message_id=int(res["message_id"]), proposal_id=p.id, user_id=user.id))
            sent += 1
    return sent


def close_messages(db: Session, api: Any) -> int:
    """Ersetzt Vorschau und Knöpfe durch das Ergebnis, sobald ein Antrag abgeschlossen ist (egal auf welchem Weg)."""
    expire_due(db)
    rows = db.execute(
        select(TelegramMessage, Proposal).join(Proposal, Proposal.id == TelegramMessage.proposal_id)
        .where(TelegramMessage.closed_at.is_(None), Proposal.status != "pending")
    ).all()
    for tm, p in rows:
        tm.closed_at = utcnow()
        label = (p.preview or {}).get("action_label", p.action)
        text = f"{_STAMPS.get(p.status, esc(p.status))}\n<b>{esc(label)}</b>"
        if p.error:
            text += "\n" + esc(_trunc(p.error, 300))
        try:
            api.call("editMessageText", chat_id=int(tm.chat_id), message_id=tm.message_id, text=text, parse_mode="HTML",
                     reply_markup={"inline_keyboard": []})
        except TelegramError:
            pass  # Nachricht gelöscht oder zu alt: nicht schlimm
    return len(rows)


def send_daily_reports(db: Session, api: Any) -> int:
    settings = get_settings()
    if settings.daily_report_hour < 0:
        return 0
    now = utcnow().astimezone(ZoneInfo(settings.timezone))
    if now.hour < settings.daily_report_hour:
        return 0
    today = now.strftime("%Y-%m-%d")
    base = settings.public_url.rstrip("/")
    sent = 0
    for user in db.execute(select(User).where(User.telegram_chat_id.is_not(None), User.disabled.is_(False))).scalars().all():
        if user.last_report_date == today:
            continue
        user.last_report_date = today
        rows = db.execute(
            select(AutoAction).join(MailboxAccess, MailboxAccess.mailbox_id == AutoAction.mailbox_id).where(
                MailboxAccess.user_id == user.id, AutoAction.created_at >= utcnow() - timedelta(hours=24), AutoAction.undone_at.is_(None))
        ).scalars().all()
        if not rows:
            continue
        counts: dict[str, int] = {}
        for r in rows:
            counts[r.action] = counts.get(r.action, 0) + 1
        parts = ", ".join(f"{n} {_CLEANUP_NAMES.get(a, a)}" for a, n in sorted(counts.items(), key=lambda kv: -kv[1]))
        text = f"🧹 <b>Heute aufgeräumt:</b> {len(rows)} Mails\n{esc(parts)}"
        markup = {"inline_keyboard": [[{"text": "↩️ Ansehen und rückgängig machen", "url": f"{base}/cleanup"}]]} if base.startswith("https://") else None
        try:
            params: dict[str, Any] = {"chat_id": int(user.telegram_chat_id), "text": text, "parse_mode": "HTML"}
            if markup:
                params["reply_markup"] = markup
            api.call("sendMessage", **params)
            sent += 1
        except TelegramError as exc:
            log.warning("Telegram: Tagesbericht fehlgeschlagen: %s", exc)
    return sent


# ------------------------------------------------------------------ Verbinden

def create_pairing(db: Session, user: User) -> str:
    for old in db.execute(select(TelegramPairing).where(TelegramPairing.user_id == user.id)).scalars():
        db.delete(old)
    code = secrets.token_hex(4).upper()
    db.add(TelegramPairing(code_hash=sha256_hex(code), user_id=user.id, expires_at=utcnow() + timedelta(minutes=10)))
    return code


def unlink(db: Session, user: User) -> None:
    user.telegram_chat_id = None
    audit.log(db, "telegram.unlinked", actor_type="user", actor_id=user.id)


# ------------------------------------------------------------------ Eingehende Nachrichten

def handle_update(db: Session, api: Any, update: dict[str, Any]) -> None:
    if "callback_query" in update:
        _handle_callback(db, api, update["callback_query"])
    elif "message" in update:
        _handle_message(db, api, update["message"])


def _user_for(db: Session, telegram_id: Any) -> User | None:
    return db.execute(select(User).where(User.telegram_chat_id == str(telegram_id), User.disabled.is_(False))).scalar_one_or_none()


def _say(api: Any, chat_id: str, text: str) -> None:
    try:
        api.call("sendMessage", chat_id=int(chat_id), text=text, parse_mode="HTML", disable_web_page_preview=True)
    except TelegramError:
        pass


def _handle_message(db: Session, api: Any, msg: dict[str, Any]) -> None:
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return
    chat_id = str(chat.get("id"))
    text = (msg.get("text") or "").strip()

    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        code = parts[1].strip().upper() if len(parts) > 1 else ""
        if not code:
            _say(api, chat_id, "Hallo! Öffne in LokyyMail <b>Konto → Telegram verbinden</b> und sende mir dann den Code: <code>/start CODE</code>")
            return
        if not _limiter.hit(f"pair|{chat_id}"):
            _say(api, chat_id, "Zu viele Versuche. Bitte warte ein paar Minuten.")
            return
        pairing = db.get(TelegramPairing, sha256_hex(code))
        if pairing is None or pairing.expires_at < utcnow():
            _say(api, chat_id, "Dieser Code ist ungültig oder abgelaufen. Erzeuge in LokyyMail einen neuen.")
            return
        user = db.get(User, pairing.user_id)
        taken = db.execute(select(User).where(User.telegram_chat_id == chat_id, User.id != user.id)).scalar_one_or_none()
        if taken is not None:
            _say(api, chat_id, "Dieser Telegram-Account ist schon mit einem anderen Konto verbunden. Trenne ihn dort zuerst.")
            return
        user.telegram_chat_id = chat_id
        db.delete(pairing)
        audit.log(db, "telegram.linked", actor_type="user", actor_id=user.id)
        _say(api, chat_id, f"✅ Verbunden mit <b>{esc(user.email)}</b>. Ab jetzt kommen Freigaben hierher.")
        return

    user = _user_for(db, chat_id)
    if user is None:
        audit.log(db, "telegram.unknown_chat", actor_type="system")
        _say(api, chat_id, "Dieses Telegram-Konto ist nicht verbunden. Öffne in LokyyMail <b>Konto → Telegram verbinden</b>.")
        return

    compact = re.sub(r"\s", "", text)
    if not re.fullmatch(r"\d{6}", compact):
        _say(api, chat_id, "So gibst du etwas Wichtiges frei: Antworte auf die Nachricht des Antrags mit deinem 6-stelligen Code. "
                           "Einfache Freigaben gehen per Knopf.")
        return

    # Code-Antwort: erst den Code aus dem Chat entfernen, dann prüfen
    def drop_code_message() -> None:
        try:
            api.call("deleteMessage", chat_id=int(chat_id), message_id=msg["message_id"])
        except TelegramError:
            pass

    pid: str | None = None
    reply = msg.get("reply_to_message")
    if reply:
        tm = db.execute(select(TelegramMessage).where(TelegramMessage.chat_id == chat_id, TelegramMessage.message_id == reply.get("message_id"))).scalar_one_or_none()
        pid = tm.proposal_id if tm else None
    if pid is None:
        waiting = db.execute(
            select(Proposal.id).join(TelegramMessage, TelegramMessage.proposal_id == Proposal.id).where(
                TelegramMessage.chat_id == chat_id, Proposal.status == "pending", Proposal.risk_level == "high")
        ).scalars().all()
        waiting = list(dict.fromkeys(waiting))
        if not waiting:
            drop_code_message()
            _say(api, chat_id, "Es wartet nichts, was einen Code braucht.")
            return
        if len(waiting) > 1:
            _say(api, chat_id, "Es warten mehrere Anträge. Antworte direkt auf die Nachricht des Antrags, den du freigeben willst.")
            return
        pid = waiting[0]

    drop_code_message()
    if not _limiter.hit(f"code|{user.id}"):
        _say(api, chat_id, "Zu viele Versuche. Bitte warte ein paar Minuten.")
        return
    try:
        p = approve(db, proposal_id=pid, user=user, via="telegram", code=compact)
    except ProposalError as exc:
        _say(api, chat_id, "⚠️ " + esc(str(exc)))
        return
    if p.status != "executed":
        _say(api, chat_id, "⚠️ " + esc(p.error or "Nicht ausgeführt."))


def _handle_callback(db: Session, api: Any, cb: dict[str, Any]) -> None:
    def answer(text: str, alert: bool = False) -> None:
        try:
            api.call("answerCallbackQuery", callback_query_id=cb.get("id"), text=text[:190], show_alert=alert)
        except TelegramError:
            pass

    user = _user_for(db, (cb.get("from") or {}).get("id"))
    if user is None:
        audit.log(db, "telegram.foreign_click", actor_type="system")
        answer("Nicht erlaubt.", True)
        return
    match = re.fullmatch(r"([ar]):([0-9a-f]{32})", cb.get("data") or "")
    if not match:
        answer("Unbekannte Aktion.")
        return
    kind, pid = match.groups()
    try:
        if kind == "r":
            reject(db, proposal_id=pid, user=user, via="telegram")
            answer("Abgelehnt.")
        else:
            p = approve(db, proposal_id=pid, user=user, via="telegram")
            answer("Erledigt ✅" if p.status == "executed" else (p.error or "Nicht ausgeführt.")[:190], p.status != "executed")
    except ProposalError as exc:
        if exc.code == "code_required":
            answer("Das ist wichtig: Antworte auf die Nachricht mit deinem 6-stelligen Code.", True)
        else:
            answer(str(exc), True)


# ------------------------------------------------------------------ Schleifen im Worker

def run_poller(stop: threading.Event) -> None:
    api = get_api()
    if api is None:
        return
    poll = get_settings().telegram_poll_timeout
    try:
        api.call("deleteWebhook")
    except TelegramError as exc:
        log.error("Telegram-Start: %s", exc)
    offset: int | None = None
    log.info("Telegram-Bot läuft")
    while not stop.is_set():
        params: dict[str, Any] = {"timeout": poll, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        try:
            updates = api.call("getUpdates", http_timeout=poll + 10, **params)
        except TelegramError as exc:
            log.warning("Telegram: %s", exc)
            stop.wait(5)
            continue
        for update in updates or []:
            offset = update["update_id"] + 1
            try:
                with session_scope() as db:
                    handle_update(db, api, update)
            except Exception:
                log.exception("Fehler bei einem Telegram-Update")


def run_notifier(stop: threading.Event) -> None:
    api = get_api()
    if api is None:
        return
    ticks = 0
    while not stop.is_set():
        try:
            with session_scope() as db:
                notify_pending(db, api)
                close_messages(db, api)
                if ticks % 30 == 0:
                    send_daily_reports(db, api)
        except Exception:
            log.exception("Fehler im Telegram-Benachrichtiger")
        ticks += 1
        stop.wait(2)
