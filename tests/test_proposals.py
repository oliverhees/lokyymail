from datetime import timedelta

import pytest
from sqlalchemy import select

from conftest import fresh_code
from lokyymail import proposals as P
from lokyymail.models import AuditEvent, Mailbox, MailboxAccess, Proposal, User, utcnow
from lokyymail.providers.demo import DemoProvider


def _user(db, ids):
    return db.get(User, ids["user"])


def _create(app_env, ids, action, params, via="ai"):
    with app_env.session_scope() as db:
        p = P.create_proposal(db, user=_user(db, ids), mailbox_id=ids["mailbox"], action=action, params=params, via=via, key_id="k1")
        return p.id


def _approve(app_env, ids, pid, via="web", code=None, second=True):
    with app_env.session_scope() as db:
        return P.approve(db, proposal_id=pid, user=_user(db, ids), via=via, code=code, web_second_factor_ok=second).status


def _status(app_env, pid):
    with app_env.session_scope() as db:
        return db.get(Proposal, pid)


def test_low_risk_archive_via_web(app_env, world):
    pid = _create(app_env, world, "archive", {"message_id": "m3"})
    assert _status(app_env, pid).risk_level == "low"
    assert _approve(app_env, world, pid) == "executed"
    assert "INBOX" not in DemoProvider(world["mailbox"], "chef@lokyy-demo.de").get_message("m3").labels


def test_reply_to_external_is_high_risk_and_needs_code(app_env, world):
    pid = _create(app_env, world, "reply", {"message_id": "m1", "body": "Donnerstag passt."})
    p = _status(app_env, pid)
    assert p.risk_level == "high" and any("extern" in r for r in p.risk_reasons)
    assert p.preview["to"] == ["anna@kunde-beispiel.de"]
    with pytest.raises(P.ProposalError) as err:
        _approve(app_env, world, pid)
    assert err.value.code == "code_required"
    code = fresh_code(app_env, world["user"], world["secret"])
    assert _approve(app_env, world, pid, code=code) == "executed"


def test_device_always_needs_code(app_env, world, monkeypatch):
    from lokyymail.config import get_settings
    monkeypatch.setattr(get_settings(), "hermes_approvals", True)
    pid = _create(app_env, world, "mark_read", {"message_id": "m1"})
    with pytest.raises(P.ProposalError) as err:
        _approve(app_env, world, pid, via="device")
    assert err.value.code == "code_required"
    with pytest.raises(P.ProposalError) as err:
        _approve(app_env, world, pid, via="device", code="000000")
    assert err.value.code == "code_invalid"
    code = fresh_code(app_env, world["user"], world["secret"])
    assert _approve(app_env, world, pid, via="device", code=code) == "executed"


def test_web_without_second_factor_cannot_approve(app_env, world):
    pid = _create(app_env, world, "archive", {"message_id": "m3"})
    with pytest.raises(P.ProposalError):
        _approve(app_env, world, pid, second=False)


def test_forward_of_injection_mail_is_flagged(app_env, world):
    pid = _create(app_env, world, "forward", {"message_id": "m2", "to": ["archiv@datensammler-beispiel.net"]})
    reasons = " ".join(_status(app_env, pid).risk_reasons)
    assert "Weiterleitung" in reasons and "versteckten Inhalt" in reasons and "Anweisungen an die KI" in reasons


def test_changed_mail_is_not_touched(app_env, world):
    pid = _create(app_env, world, "archive", {"message_id": "m3"})
    DemoProvider(world["mailbox"], "x").modify_labels("m3", ["Label_Kunden"], [])  # Mail ändert sich danach
    assert _approve(app_env, world, pid) == "superseded"
    assert "INBOX" in DemoProvider(world["mailbox"], "x").get_message("m3").labels


def test_new_mail_in_thread_supersedes_reply(app_env, world):
    pid = _create(app_env, world, "reply", {"message_id": "m3", "body": "Bestätigt."})
    prov = DemoProvider(world["mailbox"], "chef@lokyy-demo.de")
    from lokyymail.providers.base import OutgoingMail
    prov.send(OutgoingMail(to=["info@lokyy-demo.de"], cc=[], subject="Nachtrag", body="x", thread_id="t3"))
    code = fresh_code(app_env, world["user"], world["secret"])
    assert _approve(app_env, world, pid, code=code) == "superseded"


def test_approve_only_once(app_env, world):
    pid = _create(app_env, world, "archive", {"message_id": "m3"})
    assert _approve(app_env, world, pid) == "executed"
    with pytest.raises(P.ProposalError) as err:
        _approve(app_env, world, pid)
    assert err.value.code == "not_pending"


def test_tampered_payload_is_refused(app_env, world):
    pid = _create(app_env, world, "send", {"to": ["kollege@lokyy-demo.de"], "subject": "Hi", "body": "Original"})
    with app_env.session_scope() as db:
        p = db.get(Proposal, pid)
        p.payload = {**p.payload, "to": ["angreifer@example.net"]}
    with pytest.raises(P.ProposalError) as err:
        _approve(app_env, world, pid)
    assert err.value.code == "tampered"


def test_expiry_and_scrubbing(app_env, world):
    pid = _create(app_env, world, "send", {"to": ["kollege@lokyy-demo.de"], "subject": "Geheim-Betreff", "body": "Geheimer Text"})
    with app_env.session_scope() as db:
        db.get(Proposal, pid).expires_at = utcnow() - timedelta(minutes=1)
        assert P.expire_due(db) == 1
    p = _status(app_env, pid)
    assert p.status == "expired"
    assert "Geheim" not in str(p.preview) and "Geheim" not in str(p.payload)


def test_content_is_scrubbed_after_execution(app_env, world):
    pid = _create(app_env, world, "send", {"to": ["kollege@lokyy-demo.de"], "subject": "Interna", "body": "Vertraulich"})
    assert _approve(app_env, world, pid) == "executed"
    p = _status(app_env, pid)
    assert p.result["sent_id"] and "Vertraulich" not in str(p.preview)


def test_audit_never_contains_mail_content(app_env, world):
    pid = _create(app_env, world, "send", {"to": ["kollege@lokyy-demo.de"], "subject": "Projekt Falke", "body": "Streng geheim"})
    _approve(app_env, world, pid)
    with app_env.session_scope() as db:
        dump = str([(e.event, e.detail) for e in db.execute(select(AuditEvent)).scalars()])
    assert "Falke" not in dump and "Streng" not in dump and "kollege@" not in dump
    assert "proposal.executed" in dump


def test_reader_cannot_approve(app_env, world):
    from lokyymail.security import hash_password
    with app_env.session_scope() as db:
        reader = User(email="leser@lokyy-demo.de", password_hash=hash_password("x"), role="member")
        db.add(reader)
        db.flush()
        db.add(MailboxAccess(user_id=reader.id, mailbox_id=world["mailbox"], role="reader"))
        reader_id = reader.id
    pid = _create(app_env, world, "archive", {"message_id": "m3"})
    with app_env.session_scope() as db:
        with pytest.raises(P.ProposalError) as err:
            P.approve(db, proposal_id=pid, user=db.get(User, reader_id), via="web", web_second_factor_ok=True)
    assert err.value.code == "forbidden"


def test_ai_blocked_when_mailbox_ai_disabled(app_env, world):
    with app_env.session_scope() as db:
        db.get(Mailbox, world["mailbox"]).ai_enabled = False
    with pytest.raises(P.ProposalError) as err:
        _create(app_env, world, "archive", {"message_id": "m3"})
    assert err.value.status == 403


def test_rate_limit(app_env, world, monkeypatch):
    from lokyymail.config import get_settings
    monkeypatch.setattr(get_settings(), "proposal_rate_limit_per_hour", 2)
    _create(app_env, world, "archive", {"message_id": "m3"})
    _create(app_env, world, "archive", {"message_id": "m1"})
    with pytest.raises(P.ProposalError) as err:
        _create(app_env, world, "archive", {"message_id": "m2"})
    assert err.value.status == 429


def test_protected_labels_and_unknown_labels(app_env, world):
    with pytest.raises(P.ProposalError):
        _create(app_env, world, "labels", {"message_id": "m1", "add": ["TRASH"]})
    with pytest.raises(P.ProposalError):
        _create(app_env, world, "labels", {"message_id": "m1", "add": ["Gibt_es_nicht"]})
    pid = _create(app_env, world, "labels", {"message_id": "m1", "add": ["Label_Kunden"]})
    assert _approve(app_env, world, pid) == "executed"


def test_batch(app_env, world):
    pid = _create(app_env, world, "batch", {"operation": "mark_read", "message_ids": ["m1", "m2", "m3"]})
    assert _status(app_env, pid).risk_level == "low"
    assert _approve(app_env, world, pid) == "executed"


def test_ai_can_withdraw_only_own(app_env, world):
    pid = _create(app_env, world, "archive", {"message_id": "m3"})
    with app_env.session_scope() as db:
        with pytest.raises(P.ProposalError):
            P.withdraw(db, proposal_id=pid, user=_user(db, world), key_id="fremd")
        assert P.withdraw(db, proposal_id=pid, user=_user(db, world), key_id="k1").status == "withdrawn"
