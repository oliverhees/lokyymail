"""Tests für die aus Gmail Guard übernommenen Funktionen und die neuen Regeln."""

import io
import re
import zipfile

import pytest

from conftest import fresh_code
from lokyymail import proposals as P
from lokyymail.attachments import extract_text
from lokyymail.models import Mailbox, Proposal, User
from lokyymail.providers.demo import DemoProvider, simple_pdf
from test_web import MCP_HEADERS, _key, _login, _mcp, client  # noqa: F401


def _create(app_env, ids, action, params, via="ai"):
    with app_env.session_scope() as db:
        return P.create_proposal(db, user=db.get(User, ids["user"]), mailbox_id=ids["mailbox"], action=action, params=params, via=via, key_id="k1").id


def _approve(app_env, ids, pid, via="web", code=None):
    with app_env.session_scope() as db:
        return P.approve(db, proposal_id=pid, user=db.get(User, ids["user"]), via=via, code=code, web_second_factor_ok=True).status


# ------------------------------------------------------------------ Spam und Wiederherstellen

def test_spam_and_untrash(app_env, world):
    prov = DemoProvider(world["mailbox"], "chef@lokyy-demo.de")
    pid = _create(app_env, world, "spam", {"message_id": "m1"})
    assert _approve(app_env, world, pid) == "executed"
    assert "SPAM" in prov.get_message("m1").labels and "INBOX" not in prov.get_message("m1").labels

    pid = _create(app_env, world, "trash", {"message_id": "m3"})
    assert _approve(app_env, world, pid) == "executed"
    pid = _create(app_env, world, "untrash", {"message_id": "m3"})
    assert _approve(app_env, world, pid) == "executed"
    assert "TRASH" not in prov.get_message("m3").labels


def test_batch_spam(app_env, world):
    pid = _create(app_env, world, "batch", {"operation": "spam", "message_ids": ["m1", "m2"]})
    assert _approve(app_env, world, pid) == "executed"


# ------------------------------------------------------------------ Senden komplett aus

def test_send_disabled_blocks_proposals_and_execution(app_env, world):
    pending = _create(app_env, world, "send", {"to": ["kollege@lokyy-demo.de"], "subject": "x", "body": "y"})
    with app_env.session_scope() as db:
        db.get(Mailbox, world["mailbox"]).send_disabled = True
    for action, params in [("send", {"to": ["a@lokyy-demo.de"], "subject": "x", "body": "y"}),
                           ("reply", {"message_id": "m1", "body": "x"}),
                           ("forward", {"message_id": "m1", "to": ["a@lokyy-demo.de"]})]:
        with pytest.raises(P.ProposalError) as err:
            _create(app_env, world, action, params)
        assert err.value.code == "send_disabled"
    # Ein vorher angelegter Antrag wird trotzdem nicht gesendet (doppelte Absicherung)
    assert _approve(app_env, world, pending) == "failed"
    # Aufräumen bleibt möglich
    assert _approve(app_env, world, _create(app_env, world, "archive", {"message_id": "m3"})) == "executed"


def test_web_toggle_send_disabled(client, app_env, world):
    csrf = _login(client, app_env, world)
    pid = _create(app_env, world, "send", {"to": ["kollege@lokyy-demo.de"], "subject": "x", "body": "y"})
    client.post(f"/mailboxes/{world['mailbox']}/sending", data={"enabled": "0", "csrf": csrf})
    with app_env.session_scope() as db:
        assert db.get(Mailbox, world["mailbox"]).send_disabled is True
        assert db.get(Proposal, pid).status == "rejected"  # offene Sende-Anträge verfallen sofort
    r = client.post(f"/mailboxes/{world['mailbox']}/sending", data={"enabled": "1", "csrf": csrf, "code": "000000"})
    assert "fehler" in r.headers["location"]
    client.post(f"/mailboxes/{world['mailbox']}/sending", data={"enabled": "1", "csrf": csrf, "code": fresh_code(app_env, world["user"], world["secret"])})
    with app_env.session_scope() as db:
        assert db.get(Mailbox, world["mailbox"]).send_disabled is False


# ------------------------------------------------------------------ Hermes: was das Haus verlässt, nur über die Webseite

def test_device_approval_is_off_by_default_and_needs_code_when_on(app_env, world, monkeypatch):
    from lokyymail.config import get_settings
    code = fresh_code(app_env, world["user"], world["secret"])
    pid = _create(app_env, world, "send", {"to": ["kollege@lokyy-demo.de"], "subject": "x", "body": "y"})
    with pytest.raises(P.ProposalError) as err:
        _approve(app_env, world, pid, via="device", code=code)
    assert err.value.code == "hermes_disabled"
    monkeypatch.setattr(get_settings(), "hermes_approvals", True)
    with pytest.raises(P.ProposalError) as err:
        _approve(app_env, world, pid, via="device")
    assert err.value.code == "code_required"
    assert _approve(app_env, world, pid, via="device", code=fresh_code(app_env, world["user"], world["secret"])) == "executed"
    risky = _create(app_env, world, "reply", {"message_id": "m1", "body": "ok"})
    assert _approve(app_env, world, risky, via="device", code=fresh_code(app_env, world["user"], world["secret"])) == "executed"


# ------------------------------------------------------------------ Anhänge

def test_read_attachment_via_mcp_is_wrapped_and_flagged(client, app_env, world):
    ai = _key(app_env, world["user"], "ai")
    mail = _mcp(client, ai, "read_message", {"mailbox_id": world["mailbox"], "message_id": "m2"})["content"][0]["text"]
    part = re.search(r"part_id (\w+)", mail).group(1)
    text = _mcp(client, ai, "read_attachment", {"mailbox_id": world["mailbox"], "message_id": "m2", "part_id": part}, 2)["content"][0]["text"]
    assert text.startswith("<untrusted_email>") and "1.190,00 EUR" in text
    assert "Mögliche Prompt-Injection" in text


def test_attachment_formats():
    pdf_text, _ = extract_text(simple_pdf(["Hallo PDF"]), "a.pdf", "application/pdf")
    assert "Hallo PDF" in pdf_text
    html_text, hint = extract_text(b'<p>sichtbar</p><div style="display:none">geheim</div>', "a.html", "text/html")
    assert "sichtbar" in html_text and "geheim" not in html_text and hint
    assert extract_text(b"\x00\x01", "bild.png", "image/png")[0] is None
    assert extract_text(b"kaputt", "x.pdf", "application/pdf")[0] is None


def test_docx_zip_bomb_is_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b"0" * (60 * 1024 * 1024))
    text, hint = extract_text(buf.getvalue(), "bombe.docx", "")
    assert text is None and "Zip-Bomben" in hint


def test_mcp_lists_new_tools(client, app_env, world):
    ai = _key(app_env, world["user"], "ai")
    r = client.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {ai}"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert {"read_attachment", "propose_spam", "propose_untrash"} <= names
