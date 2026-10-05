"""Risiko-Bewertung. Hohes Risiko = strengere Freigabe (Zwei-Faktor-Code, kein Telegram)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Settings


@dataclass
class SourceFlags:
    """Auffälligkeiten der Mail, auf die sich ein Antrag bezieht."""

    hidden_content: bool = False
    injection: list[str] = field(default_factory=list)


def org_domains(settings: Settings, mailbox_address: str) -> set[str]:
    domains = set(settings.org_domains)
    if not domains and "@" in mailbox_address:
        domains.add(mailbox_address.rsplit("@", 1)[1].lower())
    return domains


def external_recipients(recipients: list[str], domains: set[str]) -> list[str]:
    return [r for r in recipients if r.rsplit("@", 1)[-1].lower() not in domains]


def assess(
    *,
    settings: Settings,
    action: str,
    mailbox_address: str,
    recipients: list[str] | None = None,
    attachments: bool = False,
    batch_count: int = 0,
    source: SourceFlags | None = None,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    recipients = recipients or []
    domains = org_domains(settings, mailbox_address)

    if recipients:
        external = external_recipients(recipients, domains)
        if external:
            reasons.append("1 externer Empfänger" if len(external) == 1 else f"{len(external)} externe Empfänger")
        if len(recipients) > 10:
            reasons.append(f"Viele Empfänger ({len(recipients)})")
    if action == "forward":
        reasons.append("Weiterleitung: Inhalt verlässt das Postfach")
    if attachments:
        reasons.append("Mit Anhang")
    if batch_count > settings.high_risk_batch_threshold:
        reasons.append(f"Sammelaktion über {batch_count} Mails")
    if source and source.hidden_content:
        reasons.append("Bezugs-Mail enthielt versteckten Inhalt")
    if source and source.injection:
        reasons.append("Bezugs-Mail enthielt mögliche Anweisungen an die KI")

    return ("high" if reasons else "low"), reasons
