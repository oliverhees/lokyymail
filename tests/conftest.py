"""Testumgebung: SQLite, Demo-Postfach, fester Hauptschlüssel."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pyotp
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ.update(
    LOKYY_MASTER_KEY="dGVzdC1rZXktdGVzdC1rZXktdGVzdC1rZXktMzJieXQ=",
    LOKYY_PUBLIC_URL="http://testserver",
    LOKYY_DEMO_MODE="1",
    LOKYY_ORG_DOMAINS="lokyy-demo.de",
)


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOKYY_DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    from lokyymail.config import get_settings

    get_settings.cache_clear()
    from lokyymail import db as dbmod
    from lokyymail.providers.demo import reset_demo_store
    from lokyymail.web.deps import code_limiter, login_limiter

    dbmod.init_engine()
    dbmod.create_all()
    reset_demo_store()
    login_limiter.reset()
    code_limiter.reset()
    yield dbmod
    get_settings.cache_clear()


@pytest.fixture()
def world(app_env):
    """Ein Admin mit Zwei-Faktor und ein Demo-Postfach mit KI-Zugriff."""
    from lokyymail.models import Mailbox, MailboxAccess, User
    from lokyymail.security import encrypt, hash_password

    secret = pyotp.random_base32()
    with app_env.session_scope() as db:
        user = User(email="chef@lokyy-demo.de", display_name="Chef", role="admin", password_hash=hash_password("Sicher-Passwort-123"))
        db.add(user)
        db.flush()
        user.totp_secret_enc = encrypt(secret, context=f"totp:{user.id}")
        user.totp_enabled = True
        box = Mailbox(provider="demo", address="chef@lokyy-demo.de", ai_enabled=True)
        db.add(box)
        db.flush()
        db.add(MailboxAccess(user_id=user.id, mailbox_id=box.id, role="owner"))
        ids = {"user": user.id, "mailbox": box.id, "secret": secret}
    return ids


def fresh_code(dbmod, user_id: str, secret: str) -> str:
    """Setzt den Wiederverwendungs-Schutz zurück und liefert einen gültigen Code (nur für Tests)."""
    from lokyymail.models import User

    with dbmod.session_scope() as db:
        db.get(User, user_id).totp_last_step = 0
    return pyotp.TOTP(secret).now()
