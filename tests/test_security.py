import pyotp
import pytest

from lokyymail import security


def test_encrypt_roundtrip_and_context_binding():
    token = security.encrypt("geheim", context="mailbox:a")
    assert "geheim" not in token
    assert security.decrypt(token, context="mailbox:a") == "geheim"
    with pytest.raises(Exception):
        security.decrypt(token, context="mailbox:b")  # an anderen Zweck gebunden


def test_api_key_format():
    plain, prefix, digest = security.new_api_key("ai")
    assert plain.startswith("lkai_") and security.parse_api_key(plain) == ("ai", prefix)
    assert security.sha256_hex(plain) == digest
    assert security.parse_api_key("irgendwas") is None


def test_totp_replay_is_blocked():
    secret = pyotp.random_base32()
    code = pyotp.TOTP(secret).now()
    step = security.verify_totp(secret, code, 0)
    assert step is not None
    assert security.verify_totp(secret, code, step) is None  # gleicher Code zweimal: abgelehnt


def test_password_rules():
    assert security.password_problems("kurz")
    assert security.password_problems("Lang-Genug-Passwort-1") == []
