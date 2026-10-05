"""Backend-Teil des Hermes-Plugins: Eingaben werden streng geprüft, der Schlüssel nie ausgegeben."""

import importlib.util
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PLUGIN = Path(__file__).resolve().parents[1] / "hermes-plugin" / "lokyymail" / "dashboard" / "plugin_api.py"


@pytest.fixture()
def plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("LOKYYMAIL_URL", raising=False)
    monkeypatch.delenv("LOKYYMAIL_DEVICE_TOKEN", raising=False)
    spec = importlib.util.spec_from_file_location("lokyy_plugin_api", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/lokyymail")
    return mod, TestClient(app), tmp_path


def test_unconfigured(plugin):
    _, c, _ = plugin
    assert c.get("/api/plugins/lokyymail/config").json() == {"configured": False, "url": ""}
    assert c.get("/api/plugins/lokyymail/status").status_code == 409


def test_config_validation(plugin):
    _, c, _ = plugin
    r = c.post("/api/plugins/lokyymail/config", json={"url": "http://boese.example", "token": "lkdv_x_y"})
    assert r.status_code == 422  # kein https
    r = c.post("/api/plugins/lokyymail/config", json={"url": "https://mail.firma.de", "token": "lkai_x_y"})
    assert r.status_code == 422  # KI-Schlüssel statt Geräte-Schlüssel


def test_token_never_returned_and_ids_checked(plugin):
    mod, c, home = plugin
    (home / "lokyymail.json").write_text(json.dumps({"url": "https://mail.firma.de", "token": "lkdv_geheim_123"}))
    body = c.get("/api/plugins/lokyymail/config").json()
    assert body == {"configured": True, "url": "https://mail.firma.de"} and "geheim" not in json.dumps(body)
    assert c.get("/api/plugins/lokyymail/mailboxes/..%2Fadmin/messages").status_code in (404, 422)
    assert c.post("/api/plugins/lokyymail/proposals/a$b/reject").status_code == 422
