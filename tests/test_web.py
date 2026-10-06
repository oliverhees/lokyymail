import json
import re

import pytest
from fastapi.testclient import TestClient

from conftest import fresh_code
from lokyymail.models import ApiKey, Proposal
from lokyymail.security import new_api_key

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18"}


@pytest.fixture()
def client(world):
    from lokyymail.web.app import create_app

    with TestClient(create_app(), follow_redirects=False) as c:
        yield c


def _key(app_env, user_id, kind):
    plain, prefix, digest = new_api_key(kind)
    with app_env.session_scope() as db:
        db.add(ApiKey(user_id=user_id, kind=kind, name="t", prefix=prefix, secret_hash=digest))
    return plain


def _login(client, app_env, world):
    page = client.get("/login")
    pre = re.search(r'name="pre_csrf" value="([^"]+)"', page.text).group(1)
    r = client.post("/login", data={"email": "chef@lokyy-demo.de", "password": "Sicher-Passwort-123", "pre_csrf": pre})
    assert r.status_code == 303 and r.headers["location"] == "/login/code"
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/login/code").text).group(1)
    r = client.post("/login/code", data={"code": fresh_code(app_env, world["user"], world["secret"]), "csrf": csrf})
    assert r.status_code == 303 and r.headers["location"] == "/"
    return csrf


def _mcp(client, key, name, args, rid=1):
    r = client.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {key}"},
                    json={"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name, "arguments": args}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


# ------------------------------------------------------------------ Web

def test_pages_require_login_and_second_factor(client, app_env, world):
    assert client.get("/").headers["location"] == "/login"
    page = client.get("/login")
    pre = re.search(r'name="pre_csrf" value="([^"]+)"', page.text).group(1)
    client.post("/login", data={"email": "chef@lokyy-demo.de", "password": "Sicher-Passwort-123", "pre_csrf": pre})
    assert client.get("/").headers["location"] == "/login/code"  # Passwort allein reicht nicht


def test_wrong_password_and_missing_csrf(client):
    r = client.post("/login", data={"email": "chef@lokyy-demo.de", "password": "falsch"})
    assert r.status_code == 400  # ohne Formular-Token
    page = client.get("/login")
    pre = re.search(r'name="pre_csrf" value="([^"]+)"', page.text).group(1)
    r = client.post("/login", data={"email": "chef@lokyy-demo.de", "password": "falsch", "pre_csrf": pre})
    assert r.status_code == 401


def test_security_headers(client):
    r = client.get("/login")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["referrer-policy"] == "no-referrer"


def test_full_web_approval(client, app_env, world):
    csrf = _login(client, app_env, world)
    # Interne Antwort = Stufe "normal": Vorschau, ein Klick, kein Code
    r = client.post(f"/mailboxes/{world['mailbox']}/messages/m3/propose", data={"action": "reply", "body": "Bestätigt.", "csrf": csrf})
    assert r.status_code == 303
    pid = r.headers["location"].rsplit("/", 1)[1]
    page = client.get(f"/proposals/{pid}")
    assert "Freigeben und senden" in page.text and 'name="code"' not in page.text
    assert client.post(f"/proposals/{pid}/approve", data={}).status_code == 400  # CSRF fehlt
    assert client.post(f"/proposals/{pid}/approve", data={"csrf": csrf}).status_code == 303
    with app_env.session_scope() as db:
        assert db.get(Proposal, pid).status == "executed"


def test_cleanup_click_on_web_runs_immediately(client, app_env, world):
    csrf = _login(client, app_env, world)
    r = client.post(f"/mailboxes/{world['mailbox']}/messages/m3/propose", data={"action": "archive", "csrf": csrf})
    assert r.status_code == 303 and "info=" in r.headers["location"]  # sofort erledigt, dein Klick war die Freigabe

def test_high_risk_shows_airmail_and_external_marker(client, app_env, world):
    csrf = _login(client, app_env, world)
    r = client.post(f"/mailboxes/{world['mailbox']}/messages/m1/propose", data={"action": "reply", "body": "Gern!", "csrf": csrf})
    page = client.get(r.headers["location"]).text
    assert 'class="letter high"' in page and 'class="external"' in page and 'name="code"' in page


def test_message_view_warns_about_hidden_text(client, app_env, world):
    _login(client, app_env, world)
    page = client.get(f"/mailboxes/{world['mailbox']}/messages/m2").text
    assert "versteckten Text" in page and 'sandbox=""' in page


def test_audit_csv_for_admin(client, app_env, world):
    _login(client, app_env, world)
    r = client.get("/audit.csv")
    assert r.status_code == 200 and "auth.login" in r.text


# ------------------------------------------------------------------ Schlüssel-Trennung

def test_ai_key_cannot_use_device_api(client, app_env, world):
    ai = _key(app_env, world["user"], "ai")
    r = client.get("/api/device/status", headers={"Authorization": f"Bearer {ai}"})
    assert r.status_code == 403


def test_device_key_cannot_use_mcp(client, app_env, world):
    dev = _key(app_env, world["user"], "device")
    r = client.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {dev}"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert r.status_code == 403


def test_revoked_key(client, app_env, world):
    dev = _key(app_env, world["user"], "device")
    with app_env.session_scope() as db:
        for k in db.query(ApiKey).all():
            k.revoked = True
    assert client.get("/api/device/status", headers={"Authorization": f"Bearer {dev}"}).status_code == 401


def test_device_flow_needs_code(client, app_env, world, monkeypatch):
    from lokyymail.config import get_settings
    monkeypatch.setattr(get_settings(), "hermes_approvals", True)
    dev = _key(app_env, world["user"], "device")
    h = {"Authorization": f"Bearer {dev}"}
    st = client.get("/api/device/status", headers=h).json()
    assert st["mailboxes"][0]["id"] == world["mailbox"]
    p = client.post(f"/api/device/mailboxes/{world['mailbox']}/proposals", headers=h, json={"action": "trash", "params": {"message_id": "m3"}}).json()
    r = client.post(f"/api/device/proposals/{p['id']}/approve", headers=h, json={"code": "123456"})
    assert r.status_code == 403
    r = client.post(f"/api/device/proposals/{p['id']}/approve", headers=h, json={"code": fresh_code(app_env, world["user"], world["secret"])})
    assert r.status_code == 200 and r.json()["status"] == "executed"


# ------------------------------------------------------------------ MCP

def test_mcp_has_no_approval_tools(client, app_env, world):
    ai = _key(app_env, world["user"], "ai")
    r = client.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {ai}"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert "propose_reply" in names and "read_message" in names
    assert not any(word in n for n in names for word in ("approve", "execute", "confirm", "send_now", "key", "setting"))


def test_mcp_read_is_wrapped_and_hides_attack(client, app_env, world):
    ai = _key(app_env, world["user"], "ai")
    text = _mcp(client, ai, "read_message", {"mailbox_id": world["mailbox"], "message_id": "m2"})["content"][0]["text"]
    assert text.startswith("<untrusted_email>") and "Versteckter Inhalt wurde entfernt" in text
    assert "datensammler" not in text  # die versteckte Anweisung erreicht die KI nicht


def test_mcp_propose_then_human_decides(client, app_env, world):
    ai = _key(app_env, world["user"], "ai")
    res = _mcp(client, ai, "propose_reply", {"mailbox_id": world["mailbox"], "message_id": "m1", "body": "Donnerstag 10 Uhr passt.", "reason": "Termin bestätigen"})
    data = json.loads(res["content"][0]["text"])
    assert data["status"] == "pending" and data["risk_level"] == "high"
    status = json.loads(_mcp(client, ai, "get_proposal_status", {"proposal_id": data["proposal_id"]}, 2)["content"][0]["text"])
    assert status["status"] == "pending"


def test_mcp_without_key(client, world):
    r = client.post("/mcp", headers=MCP_HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert r.status_code == 401
    r = client.post("/mcp", headers={**MCP_HEADERS, "Authorization": "Bearer lkai_abcd1234_falsch"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert r.status_code == 401


def test_mcp_rejects_foreign_host(client, app_env, world):
    ai = _key(app_env, world["user"], "ai")
    r = client.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {ai}", "Host": "evil.example"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert r.status_code in (400, 403, 421)


def test_mcp_connect_guide(client, app_env, world):
    csrf = _login(client, app_env, world)
    page = client.get("/keys")
    assert page.status_code == 200
    assert "KI verbinden (MCP)" in page.text and "lkai_DEIN_SCHLÜSSEL" in page.text
    assert "http://lokyymail:8080/mcp" in page.text
    assert "/static/copy.js" in page.text

    r = client.post("/keys", data={"kind": "ai", "name": "Hermes", "csrf": csrf})
    assert r.status_code == 200
    key = re.search(r'class="keybox">(lkai_[^<]+)<', r.text).group(1)
    assert f"Bearer {key}" in r.text and "claude mcp add" in r.text and "lkai_DEIN_SCHLÜSSEL" not in r.text

    # Gerätezugang bekommt die MCP-Anleitung nicht
    r = client.post("/keys", data={"kind": "device", "name": "Desktop", "csrf": csrf})
    assert "KI verbinden (MCP)" not in r.text

    assert client.get("/static/copy.js").status_code == 200
