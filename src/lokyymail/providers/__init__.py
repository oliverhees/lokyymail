"""Mail-Anschlüsse. ``provider_for`` liefert den passenden Anschluss für ein Postfach."""

from __future__ import annotations

from typing import Any

from ..config import get_settings
from ..models import Mailbox
from ..security import decrypt_json
from .base import MailProvider, MessageDetail, MessageSummary, OutgoingMail, ProviderError
from .demo import DemoProvider

__all__ = ["MailProvider", "MessageDetail", "MessageSummary", "OutgoingMail", "ProviderError", "provider_for"]

# Tests können hier einen eigenen Anschluss einsetzen.
_override: dict[str, Any] = {}


def credentials_context(mailbox_id: str) -> str:
    return f"mailbox:{mailbox_id}"


def provider_for(mailbox: Mailbox) -> MailProvider:
    if mailbox.id in _override:
        return _override[mailbox.id]
    if mailbox.status == "disconnected":
        raise ProviderError("Dieses Postfach ist getrennt. Bitte neu verbinden.")
    if mailbox.provider == "demo":
        return DemoProvider(mailbox.id, mailbox.address)
    if mailbox.provider == "gmail":
        from .gmail import GmailProvider

        settings = get_settings()
        if not mailbox.credentials_enc:
            raise ProviderError("Für dieses Postfach fehlen die Zugangsdaten.")
        creds = decrypt_json(mailbox.credentials_enc, context=credentials_context(mailbox.id))
        return GmailProvider(
            address=mailbox.address,
            credentials=creds,
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
        )
    raise ProviderError(f"Unbekannter Anbieter: {mailbox.provider}")
