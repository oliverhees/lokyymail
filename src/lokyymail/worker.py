"""Hintergrund-Aufgaben: Anträge ablaufen lassen, aufräumen, Telegram-Bot betreiben.

Wichtig: Nur EIN Worker pro Instanz laufen lassen (der Telegram-Bot fragt per Long-Polling ab).
"""

from __future__ import annotations

import logging
import signal
import threading
from datetime import timedelta

from sqlalchemy import delete

from .config import get_settings
from .db import create_all, init_engine, session_scope
from .models import AuditEvent, AutoAction, OAuthState, TelegramMessage, TelegramPairing, WebSession, utcnow
from .proposals import expire_due

log = logging.getLogger("lokyymail.worker")


def run_once() -> dict[str, int]:
    settings = get_settings()
    now = utcnow()
    with session_scope() as db:
        expired = expire_due(db)
        sessions = db.execute(delete(WebSession).where(WebSession.expires_at < now)).rowcount or 0
        states = db.execute(delete(OAuthState).where(OAuthState.created_at < now - timedelta(hours=1))).rowcount or 0
        pairings = db.execute(delete(TelegramPairing).where(TelegramPairing.expires_at < now)).rowcount or 0
        tg_old = db.execute(delete(TelegramMessage).where(TelegramMessage.created_at < now - timedelta(days=14))).rowcount or 0
        undo = db.execute(delete(AutoAction).where(AutoAction.created_at < now - timedelta(days=settings.undo_days))).rowcount or 0
        old_audit = db.execute(delete(AuditEvent).where(AuditEvent.ts < now - timedelta(days=settings.audit_retention_days))).rowcount or 0
    return {"expired": expired, "sessions": sessions, "oauth_states": states, "pairings": pairings,
            "telegram_old": tg_old, "undo_list": undo, "audit": old_audit}


def run_forever(interval: int = 30) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_engine()
    create_all()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    if get_settings().telegram_bot_token:
        from . import telegram

        for fn in (telegram.run_poller, telegram.run_notifier):
            threading.Thread(target=fn, args=(stop,), daemon=True, name=fn.__name__).start()
        log.info("Telegram aktiv")
    else:
        log.info("Telegram nicht eingerichtet (LOKYY_TELEGRAM_BOT_TOKEN fehlt)")

    log.info("Worker gestartet")
    while not stop.is_set():
        try:
            result = run_once()
            if any(result.values()):
                log.info("Aufgeräumt: %s", result)
        except Exception:  # Worker darf nicht sterben
            log.exception("Fehler im Worker-Durchlauf")
        stop.wait(interval)
    log.info("Worker beendet")
