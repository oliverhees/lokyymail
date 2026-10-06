# 🚢 LokyyMail in Coolify einrichten

Eine Instanz pro Kunde, auf dem Server des Kunden. Dauer: ca. 20 Minuten.

> ⚠️ **Vorher klären:** Hermes darf **keinen** Zugang zu Coolify, Docker oder dieser Datenbank haben.
> Wer an den Server kommt, kommt an alle Schlüssel. Am besten läuft Hermes auf einer anderen Maschine.

## 1. Ressource anlegen

1. Coolify → Projekt des Kunden → **+ New** → **Docker Compose** → dieses Repo.
2. Compose-Datei: `docker-compose.yml`.
3. Unter **Domains** beim Dienst `lokyymail` die Kunden-Domain eintragen, z. B. `https://mail.kunde.de` (Port 8080).

✅ **Fertig, wenn** Coolify die drei Dienste `lokyymail`, `worker`, `postgres` anzeigt.

## 2. Umgebungsvariablen

| Variable | Pflicht | Wert |
|---|---|---|
| `LOKYY_PUBLIC_URL` | ✅ | die Domain aus Schritt 1, mit `https://` |
| `LOKYY_MASTER_KEY` | ✅ | `openssl rand -base64 32` – **sicher aufbewahren!** Ohne ihn sind alle Zugangsdaten verloren. |
| `POSTGRES_PASSWORD` | ✅ | ein langes Zufallspasswort |
| `LOKYY_ORG_DOMAINS` | empfohlen | Firmen-Domains, z. B. `kunde.de,kunde.com` |
| `LOKYY_GOOGLE_CLIENT_ID` / `_SECRET` | für Gmail | siehe [google-workspace.md](google-workspace.md) |

Dann **Deploy**.

✅ **Fertig, wenn** `https://mail.kunde.de/health` die Antwort `{"status":"ok",…}` zeigt.

## 3. Admin anlegen

Coolify → Dienst `lokyymail` → **Terminal**:

```bash
lokyymail create-admin --email chef@kunde.de --name "Vorname Nachname"
```

Passwort eingeben (mind. 12 Zeichen, Groß/klein, Ziffer).

✅ **Fertig, wenn** der Login auf `https://mail.kunde.de` klappt und du Zwei-Faktor eingerichtet hast.

## 4. Postfach verbinden

Weboberfläche → **Postfächer** → **Mit Google Workspace verbinden** → mit dem Firmenkonto anmelden.
Danach **KI-Zugriff erlauben** (startet bewusst ausgeschaltet).

✅ **Fertig, wenn** das Postfach mit „KI darf lesen“ in der Übersicht steht.

## 5. Weitere Mitarbeiter

Weboberfläche → **Nutzer** → anlegen. Jeder verbindet sein eigenes Postfach selbst.

## Erreichbarkeit für Hermes im selben Docker-Netz

Läuft MetaMCP oder Hermes im selben Coolify, kann es den MCP-Server intern unter `http://lokyymail:8080/mcp`
erreichen. Dafür ist `LOKYY_MCP_EXTRA_HOSTS=lokyymail:8080` voreingestellt. Liegen die Dienste in
verschiedenen Netzen, hilft der Netzwerk-Trick aus Gmail Guard (`scripts/connect-metamcp.sh`) sinngemäß.

## 6. Telegram (empfohlen)
Siehe [telegram.md](telegram.md). Freigaben aufs Handy, einfache mit einem Tipp.

## Update auf eine neue Version
1. **Backup** der Datenbank (Volume `lokyymail-db`).
2. In Coolify **Deploy** (neues Image). Neue Tabellen und Spalten legt LokyyMail beim Start selbst an, Daten bleiben erhalten.
3. Neue Einstellungen stehen in `.env.example`.

## Backup

- Datenbank-Volume `lokyymail-db` sichern.
- `LOKYY_MASTER_KEY` getrennt davon sicher aufbewahren (Passwort-Manager).
