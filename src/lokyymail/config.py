"""Zentrale Konfiguration. Alle Werte kommen aus Umgebungsvariablen (Präfix LOKYY_)."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOKYY_", env_file=".env", extra="ignore")

    # Grundlagen
    public_url: str = "http://localhost:8080"
    database_url: str = "sqlite:///./lokyymail.db"
    # 32 Byte als Base64 (urlsafe). Erzeugen mit: lokyymail generate-key
    master_key: str = Field(default="", repr=False)

    # Firma: eigene Domains. Empfänger außerhalb gelten als extern (= hohes Risiko).
    org_domains: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Zusätzliche Host-Namen, unter denen der MCP-Server erreichbar ist (z. B. interner Docker-Name "lokyymail:8080")
    mcp_extra_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Google Workspace (eigenes Projekt beim Kunden, Typ "Intern")
    google_client_id: str = ""
    google_client_secret: str = Field(default="", repr=False)

    # Freigabe-Regeln
    proposal_ttl_minutes: int = 15
    proposal_rate_limit_per_hour: int = 30
    batch_max_messages: int = 50
    high_risk_batch_threshold: int = 5

    # Sitzungen und Protokoll
    session_hours: int = 12
    audit_retention_days: int = 365

    # Anzeige-Zeitzone der Weboberfläche
    timezone: str = "Europe/Berlin"

    # Entwicklung: Demo-Postfach ohne echte Zugangsdaten
    demo_mode: bool = False

    @field_validator("org_domains", "mcp_extra_hosts", mode="before")
    @classmethod
    def _split_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [d.strip().lower() for d in value.split(",") if d.strip()]
        return value

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def public_host(self) -> str:
        return urlsplit(self.public_url).netloc

    @property
    def google_redirect_uri(self) -> str:
        return self.public_url.rstrip("/") + "/mailboxes/google/callback"


@lru_cache
def get_settings() -> Settings:
    return Settings()
