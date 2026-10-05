"""Sicherheits-Bausteine: Verschlüsselung (AES-GCM), Passwörter (Argon2), TOTP, Tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import get_settings

_KEY_VERSION = "v1"
_hasher = PasswordHasher()


class ConfigError(RuntimeError):
    pass


# ---------------------------------------------------------------- Verschlüsselung

def generate_master_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def _master_key() -> bytes:
    raw = get_settings().master_key
    if not raw:
        raise ConfigError("LOKYY_MASTER_KEY fehlt. Erzeugen mit: lokyymail generate-key")
    try:
        key = base64.urlsafe_b64decode(raw.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ConfigError("LOKYY_MASTER_KEY ist kein gültiges Base64.") from exc
    if len(key) != 32:
        raise ConfigError("LOKYY_MASTER_KEY muss genau 32 Byte lang sein.")
    return key


def encrypt(plaintext: str, *, context: str) -> str:
    """Verschlüsselt Text. 'context' bindet den Wert an seinen Zweck (z. B. Postfach-ID)."""
    nonce = secrets.token_bytes(12)
    ct = AESGCM(_master_key()).encrypt(nonce, plaintext.encode("utf-8"), context.encode("utf-8"))
    return f"{_KEY_VERSION}:" + base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def decrypt(token: str, *, context: str) -> str:
    version, _, body = token.partition(":")
    if version != _KEY_VERSION:
        raise ValueError("Unbekannte Schlüsselversion")
    raw = base64.urlsafe_b64decode(body.encode("ascii"))
    nonce, ct = raw[:12], raw[12:]
    return AESGCM(_master_key()).decrypt(nonce, ct, context.encode("utf-8")).decode("utf-8")


def encrypt_json(data: dict[str, Any], *, context: str) -> str:
    return encrypt(json.dumps(data, separators=(",", ":")), context=context)


def decrypt_json(token: str, *, context: str) -> dict[str, Any]:
    return json.loads(decrypt(token, context=context))


# ---------------------------------------------------------------- Passwörter

def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_problems(password: str) -> list[str]:
    problems = []
    if len(password) < 12:
        problems.append("Mindestens 12 Zeichen.")
    if password.lower() == password or password.upper() == password:
        problems.append("Groß- und Kleinbuchstaben mischen.")
    if not any(c.isdigit() for c in password):
        problems.append("Mindestens eine Ziffer.")
    return problems


# ---------------------------------------------------------------- TOTP (Zwei-Faktor)

def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, account: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name="LokyyMail")


def verify_totp(secret: str, code: str, last_step: int) -> int | None:
    """Prüft einen 6-stelligen Code. Gibt den Zeitschritt zurück (gegen Wiederverwendung) oder None."""
    code = "".join(ch for ch in code if ch.isdigit())
    if len(code) != 6:
        return None
    totp = pyotp.TOTP(secret)
    now_step = int(time.time()) // 30
    for offset in (-1, 0, 1):
        step = now_step + offset
        if step <= last_step:
            continue
        if hmac.compare_digest(totp.at(step * 30), code):
            return step
    return None


# ---------------------------------------------------------------- Tokens

def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_api_key(kind: str) -> tuple[str, str, str]:
    """Erzeugt (Klartext, Präfix, Hash). Der Klartext wird genau einmal angezeigt."""
    tag = {"ai": "lkai", "device": "lkdv"}[kind]
    prefix = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    plaintext = f"{tag}_{prefix}_{secret}"
    return plaintext, prefix, sha256_hex(plaintext)


def parse_api_key(plaintext: str) -> tuple[str, str] | None:
    parts = plaintext.strip().split("_", 2)
    if len(parts) != 3 or parts[0] not in ("lkai", "lkdv"):
        return None
    kind = "ai" if parts[0] == "lkai" else "device"
    return kind, parts[1]


def canonical_hash(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
