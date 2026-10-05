"""Hintergrund-Aufgaben: Anträge ablaufen lassen, Sitzungen und altes Protokoll aufräumen."""

from __future__ import annotations

import logging
import signal
import time
from datetime import timedelta

from sqlalchemy import delete

from .config import get_settings
from .db import create_all, init_engine, session_scope
from .models import AuditEvent, OAuthState, WebSession, utcnow
from .proposals import expire_due

log = logging.getLogger("lokyymail.worker")


def run_once() -> dict[str, int]:
    settings = get_settings()
    with session_scope() as db:
        expired = expire_due(db)
        sessions = db.execute(delete(WebSession).where(WebSession.expires_at < utcnow())).rowcount or 0
        states = db.execute(delete(OAuthState).where(OAuthState.created_at < utcnow() - timedelta(hours=1))).rowcount or 0
        old_audit = db.execute(
            delete(AuditEvent).where(AuditEvent.ts < utcnow() - timedelta(days=settings.audit_retention_days))
        ).rowcount or 0
    return {"expired": expired, "sessions": sessions, "oauth_states": states, "audit": old_audit}


def run_forever(interval: int = 30) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_engine()
    create_all()
    stop = False

    def _stop(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("Worker gestartet")
    while not stop:
        try:
            result = run_once()
            if any(result.values()):
                log.info("Aufgeräumt: %s", result)
        except Exception:  # Worker darf nicht sterben
            log.exception("Fehler im Worker-Durchlauf")
        for _ in range(interval):
            if stop:
                break
            time.sleep(1)
    log.info("Worker beendet")
