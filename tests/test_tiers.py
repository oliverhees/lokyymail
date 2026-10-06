"""Drei Stufen: Aufräumen automatisch, normal = 1 Tipp, wichtig = Code. Dazu Rückgängig, Telegram, Migration."""

import pytest
from sqlalchemy import select

from conftest import fresh_code
from lokyymail import proposals as P
from lokyymail import telegram as tg
from lokyymail.config import get_settings
from lokyymail.models import AutoAction, Mailbox, Proposal, TelegramMessage, User
from lokyymail.providers.demo import DemoProvider


class FakeTG:
    def __init__(self):
        self.calls, self._n = [], 100

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "sendMessage":
            self._n += 1
            return {"message_id": self._n}
        if method == "getMe":
            return {"username": "lokyy_test_bot"}
        return True

    def sent(self, method="sendMessage"):
        return [p for m, p in self.calls if m == method]


@pytest.fixture(autouse=True)
def _reset_limits():
    tg._limiter.reset()


def _mk(app_env, ids, action, params, via="ai"):
    with app_env.session_scope() as db:
        p = P.create_proposal(db, user=db.get(User, ids["user"]), mailbox_id=ids["mailbox"], action=action, params=params, via=via, key_id="k1")
        return p.id, p.status


def _approve(app_env, ids, pid, via="web", code=None):
    with app_env.session_scope() as db:
        return P.approve(db, proposal_id=pid, user=db.get(User, ids["user"]), via=via, code=code, web_second_factor_ok=True).status


def _cleanup_on(app_env, ids, on=True):
    with app_env.session_scope() as db:
        db.get(Mailbox, ids["mailbox"]).auto_cleanup = on


def _link(app_env, ids, chat="4711"):
    with app_env.session_scope() as db:
        db.get(User, ids["user"]).telegram_chat_id = chat


def _labels(ids, mid):
    return DemoProvider(ids["mailbox"], "chef@lokyy-demo.de").get_message(mid).labels


# ------------------------------------------------------------------ Aufräumen automatisch

def test_ai_cleanup_runs_directly_when_enabled(app_env, world):
    _cleanup_on(app_env, world)
    pid, status = _mk(app_env, world, "archive", {"message_id": "m3"})
    assert status == "executed" and "INBOX" not in _labels(world, "m3")
    with app_env.session_scope() as db:
        row = db.execute(select(AutoAction)).scalar_one()
        assert "info@" in row.sender and row.subject == "Wochenplan" and "INBOX" in row.labels_before
        assert db.get(Proposal, pid).decided_via == "auto"


def test_cleanup_needs_approval_when_disabled(app_env, world):
    assert _mk(app_env, world, "archive", {"message_id": "m3"})[1] == "pending"
    assert "INBOX" in _labels(world, "m3")


def test_hourly_budget(app_env, world, monkeypatch):
    monkeypatch.setattr(get_settings(), "auto_cleanup_per_hour", 2)
    _cleanup_on(app_env, world)
    assert [_mk(app_env, world, "archive", {"message_id": m})[1] for m in ("m1", "m2", "m3")] == ["executed", "executed", "pending"]


def test_trash_and_sending_never_automatic(app_env, world):
    _cleanup_on(app_env, world)
    assert _mk(app_env, world, "trash", {"message_id": "m3"})[1] == "pending"
    assert _mk(app_env, world, "batch", {"operation": "trash", "message_ids": ["m1", "m3"]})[1] == "pending"
    assert _mk(app_env, world, "send", {"to": ["kollege@lokyy-demo.de"], "subject": "x", "body": "y"})[1] == "pending"
    assert _mk(app_env, world, "reply", {"message_id": "m3", "body": "ok"})[1] == "pending"


def test_batch_cleanup_runs_directly(app_env, world):
    _cleanup_on(app_env, world)
    _, status = _mk(app_env, world, "batch", {"operation": "spam", "message_ids": ["m1", "m2", "m3"]})
    assert status == "executed"
    with app_env.session_scope() as db:
        assert len(db.execute(select(AutoAction)).scalars().all()) == 3


def test_human_web_click_is_the_approval(app_env, world):
    assert _mk(app_env, world, "archive", {"message_id": "m3"}, via="web")[1] == "executed"
    assert _mk(app_env, world, "trash", {"message_id": "m1"}, via="web")[1] == "executed"
    assert _mk(app_env, world, "reply", {"message_id": "m3", "body": "ok"}, via="web")[1] == "pending"
    with app_env.session_scope() as db:
        assert db.execute(select(AutoAction)).first() is None  # Eigene Klicks stehen nicht im KI-Bericht


# ------------------------------------------------------------------ Rückgängig

def test_undo(app_env, world):
    _cleanup_on(app_env, world)
    _mk(app_env, world, "archive", {"message_id": "m3"})
    _mk(app_env, world, "spam", {"message_id": "m1"})
    with app_env.session_scope() as db:
        user = db.get(User, world["user"])
        for entry, _ in P.list_auto_actions(db, user, days=7):
            P.undo_auto(db, entry_id=entry.id, user=user)
        with pytest.raises(P.ProposalError) as err:
            P.undo_auto(db, entry_id=entry.id, user=user)
        assert err.value.code == "already_undone"
    assert "INBOX" in _labels(world, "m3") and "INBOX" in _labels(world, "m1") and "SPAM" not in _labels(world, "m1")


def test_undo_requires_access(app_env, world):
    from lokyymail.security import hash_password
    _cleanup_on(app_env, world)
    _mk(app_env, world, "archive", {"message_id": "m3"})
    with app_env.session_scope() as db:
        stranger = User(email="fremd@x.de", password_hash=hash_password("x"))
        db.add(stranger)
        db.flush()
        entry_id = db.execute(select(AutoAction.id)).scalar_one()
        with pytest.raises(P.ProposalError) as err:
            P.undo_auto(db, entry_id=entry_id, user=stranger)
        assert err.value.status == 404


# ------------------------------------------------------------------ Stufen bei der Freigabe

def test_internal_reply_is_normal_and_needs_no_code(app_env, world):
    pid, status = _mk(app_env, world, "reply", {"message_id": "m3", "body": "Bestätigt."})
    with app_env.session_scope() as db:
        assert db.get(Proposal, pid).risk_level == "low"
    assert _approve(app_env, world, pid) == "executed"


# ------------------------------------------------------------------ Telegram

def _msg(text, chat=4711, reply_to=None, mid=5):
    m = {"message_id": mid, "chat": {"id": chat, "type": "private"}, "text": text}
    if reply_to:
        m["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": 1, "message": m}


def _cb(data, frm=4711):
    return {"update_id": 2, "callback_query": {"id": "cb1", "from": {"id": frm}, "data": data}}


def _handle(app_env, api, update):
    with app_env.session_scope() as db:
        tg.handle_update(db, api, update)


def test_pairing(app_env, world):
    api = FakeTG()
    with app_env.session_scope() as db:
        code = tg.create_pairing(db, db.get(User, world["user"]))
    _handle(app_env, api, _msg("/start FALSCH00"))
    assert "ungültig" in api.sent()[-1]["text"]
    _handle(app_env, api, _msg(f"/start {code}"))
    with app_env.session_scope() as db:
        assert db.get(User, world["user"]).telegram_chat_id == "4711"
    _handle(app_env, api, _msg(f"/start {code}"))  # Code gilt nur einmal
    assert "ungültig" in api.sent()[-1]["text"]


def test_notify_escapes_and_sends_once(app_env, world):
    _link(app_env, world)
    api = FakeTG()
    pid, _ = _mk(app_env, world, "send", {"to": ["kollege@lokyy-demo.de"], "subject": "Hi", "body": "<script>alert(1)</script> Hallo"})
    with app_env.session_scope() as db:
        assert tg.notify_pending(db, api) == 1
        assert tg.notify_pending(db, api) == 0
    msg = api.sent()[0]
    assert "&lt;script&gt;" in msg["text"] and "<script>" not in msg["text"]
    assert {b["callback_data"][:2] for b in msg["reply_markup"]["inline_keyboard"][0]} == {"a:", "r:"}
    with app_env.session_scope() as db:
        assert db.execute(select(TelegramMessage)).scalar_one().proposal_id == pid


def test_telegram_tap_approves_normal_and_closes_message(app_env, world):
    _link(app_env, world)
    api = FakeTG()
    pid, _ = _mk(app_env, world, "send", {"to": ["kollege@lokyy-demo.de"], "subject": "Hi", "body": "Hallo"})
    with app_env.session_scope() as db:
        tg.notify_pending(db, api)
    _handle(app_env, api, _cb(f"a:{pid}"))
    with app_env.session_scope() as db:
        assert db.get(Proposal, pid).status == "executed" and db.get(Proposal, pid).decided_via == "telegram"
        tg.close_messages(db, api)
    assert "Ausgeführt" in api.sent("editMessageText")[0]["text"]


def test_telegram_high_risk_needs_code_reply(app_env, world):
    _link(app_env, world)
    api = FakeTG()
    pid, _ = _mk(app_env, world, "reply", {"message_id": "m1", "body": "Donnerstag passt."})
    with app_env.session_scope() as db:
        tg.notify_pending(db, api)
        mid = db.execute(select(TelegramMessage)).scalar_one().message_id
    assert all(not b.get("callback_data", "").startswith("a:") for row in api.sent()[0]["reply_markup"]["inline_keyboard"] for b in row)
    _handle(app_env, api, _cb(f"a:{pid}"))  # Tipp reicht bei Wichtigem nicht
    assert api.sent("answerCallbackQuery")[-1]["show_alert"] is True
    _handle(app_env, api, _msg("000000", reply_to=mid))  # falscher Code
    with app_env.session_scope() as db:
        assert db.get(Proposal, pid).status == "pending"
    assert api.sent("deleteMessage")  # Code wird aus dem Chat entfernt
    _handle(app_env, api, _msg(fresh_code(app_env, world["user"], world["secret"]), reply_to=mid, mid=6))
    with app_env.session_scope() as db:
        assert db.get(Proposal, pid).status == "executed"


def test_telegram_ignores_strangers(app_env, world):
    _link(app_env, world)
    api = FakeTG()
    pid, _ = _mk(app_env, world, "send", {"to": ["kollege@lokyy-demo.de"], "subject": "Hi", "body": "Hallo"})
    _handle(app_env, api, _cb(f"a:{pid}", frm=999))
    assert api.sent("answerCallbackQuery")[-1]["text"] == "Nicht erlaubt."
    _handle(app_env, api, _msg("123456", chat=999))
    assert "nicht verbunden" in api.sent()[-1]["text"]
    with app_env.session_scope() as db:
        assert db.get(Proposal, pid).status == "pending"


def test_web_created_proposals_are_not_notified(app_env, world):
    _link(app_env, world)
    api = FakeTG()
    _mk(app_env, world, "reply", {"message_id": "m3", "body": "ok"}, via="web")
    with app_env.session_scope() as db:
        assert tg.notify_pending(db, api) == 0


def test_daily_report(app_env, world, monkeypatch):
    monkeypatch.setattr(get_settings(), "daily_report_hour", 0)
    _link(app_env, world)
    _cleanup_on(app_env, world)
    _mk(app_env, world, "archive", {"message_id": "m3"})
    api = FakeTG()
    with app_env.session_scope() as db:
        assert tg.send_daily_reports(db, api) == 1
        assert tg.send_daily_reports(db, api) == 0
    assert "1 archiviert" in api.sent()[0]["text"]


# ------------------------------------------------------------------ Update bestehender Installationen

def test_migration_adds_missing_columns(tmp_path, monkeypatch):
    monkeypatch.setenv("LOKYY_DATABASE_URL", f"sqlite:///{tmp_path}/old.db")
    get_settings.cache_clear()
    from lokyymail import db as dbmod

    engine = dbmod.init_engine()
    with engine.begin() as c:
        c.exec_driver_sql("CREATE TABLE mailboxes (id VARCHAR(32) PRIMARY KEY, provider VARCHAR(32), address VARCHAR(320))")
        c.exec_driver_sql("CREATE TABLE users (id VARCHAR(32) PRIMARY KEY, email VARCHAR(320))")
        c.exec_driver_sql("INSERT INTO mailboxes VALUES ('a', 'demo', 'x@y.de')")
    added = dbmod.migrate()
    assert {"mailboxes.auto_cleanup", "mailboxes.send_disabled", "users.telegram_chat_id"} <= set(added)
    with engine.begin() as c:
        assert c.exec_driver_sql("SELECT auto_cleanup FROM mailboxes").scalar() == 0  # Bestand bleibt, Standard: aus
    assert dbmod.migrate() == []
    get_settings.cache_clear()


# ------------------------------------------------------------------ Weboberfläche

def test_web_cleanup_flow(app_env, world, monkeypatch):
    import re
    from fastapi.testclient import TestClient
    from test_web import _login
    from lokyymail.web.app import create_app

    monkeypatch.setattr(get_settings(), "telegram_bot_token", "x")
    monkeypatch.setattr(tg, "_api_override", FakeTG())
    with TestClient(create_app(), follow_redirects=False) as c:
        csrf = _login(c, app_env, world)
        c.post(f"/mailboxes/{world['mailbox']}/cleanup", data={"enabled": "1", "csrf": csrf})
        _mk(app_env, world, "archive", {"message_id": "m3"})
        page = c.get("/cleanup").text
        assert "Wochenplan" in page and "Rückgängig" in page
        entry_id = re.search(r"/cleanup/(\d+)/undo", page).group(1)
        assert c.post(f"/cleanup/{entry_id}/undo", data={"csrf": csrf}).headers["location"].startswith("/cleanup?info=")
        assert "INBOX" in _labels(world, "m3")
        assert "Heute hat die KI" not in c.get("/").text  # nach dem Rückgängig nicht mehr gezählt
        acc = c.post("/account/telegram/start", data={"csrf": csrf}).text
        assert "/start " in acc and "lokyy_test_bot" in acc
