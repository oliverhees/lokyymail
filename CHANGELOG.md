# Changelog

## 0.4.0 – 06.10.2026 (Vorabversion)

Weniger Klicks, gleiche Sicherheit.

- **Drei Stufen:** Aufräumen automatisch, Normal = 1 Tipp, Wichtig = 2FA-Code (siehe docs/freigabe.md)
- **Aufräumen automatisch** pro Postfach (Standard aus): Stundenlimit, Bericht „Aufgeräumt“, Rückgängig-Knopf
- **Telegram:** Vorschau von LokyyMail, Tipp-Freigabe, Code als Antwort bei Wichtigem, Tagesbericht
- Wenn du auf der Webseite selbst klickst (Archivieren, Papierkorb …), passiert es sofort
- **Geändert:** Freigaben in Hermes sind standardmäßig aus (`LOKYY_HERMES_APPROVALS`), weil der Code durch das Hermes-Gateway läuft.
  Die frühere Regel „Senden nur auf der Webseite“ entfällt, stattdessen gelten die Stufen.
- Update von 0.3: neue Tabellen und Spalten werden beim Start ergänzt, Daten bleiben erhalten
- Worker wartet auf den Webserver (Start-Reihenfolge in docker-compose.yml)
- 75 Tests

## 0.3.0 – 05.10.2026 (Vorabversion)

- Freigabe-Engine: Antrag → Freigabe → erneute Prüfung → Ausführen → Nachprüfen, Inhalte nach Abschluss gelöscht
- Google Workspace über OAuth mit PKCE, Demo-Postfach mit Angriffs-Mail
- MCP-Server (18 Werkzeuge, keines kann freigeben), Schlüssel-Prüfung schon auf HTTP-Ebene
- Freigabe-Webseite mit Pflicht-Zwei-Faktor, Risiko-Ampel, Luftpost-Rahmen bei hohem Risiko
- Hermes-Desktop-Plugin (Postfach, Freigaben, Zähler in der Statusleiste)
- Aus Gmail Guard übernommen: Anhänge lesen (PDF, DOCX, HTML, Text), Spam, Wiederherstellen, „Senden komplett aus“
- Neu: Senden und hohes Risiko nur auf der Webseite freigebbar, nicht in Hermes
- Coolify-Compose mit Container-Härtung, 56 Tests
