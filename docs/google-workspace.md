# 📬 Google Workspace anbinden

Jeder Kunde bekommt ein **eigenes** Google-Cloud-Projekt vom Typ **„Intern“**.
Vorteil: **kein** Prüfverfahren und **keine** jährliche Sicherheitsprüfung bei Google.
Voraussetzung: Der Kunde nutzt **Google Workspace** (Firmen-Gmail). Private @gmail.com-Konten folgen in Phase 4 über IMAP.

Dauer: ca. 15 Minuten. Am besten mit dem Workspace-Admin des Kunden zusammen.

## 1. Projekt anlegen
1. [console.cloud.google.com](https://console.cloud.google.com) mit dem **Firmenkonto** öffnen.
2. Oben **Projekt auswählen** → **Neues Projekt** → Name `LokyyMail`.

✅ **Fertig, wenn** das Projekt oben ausgewählt ist.

## 2. Gmail-API aktivieren
**APIs & Dienste** → **Bibliothek** → „Gmail API“ → **Aktivieren**.

## 3. Zustimmungsbildschirm
**Google Auth Platform** (bzw. **OAuth-Zustimmungsbildschirm**):
- Nutzertyp: **Intern** ← wichtig
- App-Name: `LokyyMail`, Support-Mail: Admin-Adresse
- Bereich (Scope) hinzufügen: `https://www.googleapis.com/auth/gmail.modify`

✅ **Fertig, wenn** dort „Intern“ steht.

## 4. Zugangsdaten
**Clients** → **Client erstellen** → Typ **Webanwendung**:
- Autorisierte Weiterleitungs-URI: `https://mail.kunde.de/mailboxes/google/callback`
  (genau so, mit eurer Domain – sie steht auch in LokyyMail unter „Postfächer“)

Client-ID und Client-Secret kopieren und in Coolify eintragen:
`LOKYY_GOOGLE_CLIENT_ID`, `LOKYY_GOOGLE_CLIENT_SECRET` → neu deployen.

✅ **Fertig, wenn** unter „Postfächer“ der Knopf **„Mit Google Workspace verbinden“** erscheint.

## Gut zu wissen
- `gmail.modify` erlaubt technisch auch Senden. Der Schutz kommt aus LokyyMail (Freigabe), nicht von Google.
- Ein Mitarbeiter kann seinen Zugang jederzeit unter [myaccount.google.com/permissions](https://myaccount.google.com/permissions) widerrufen.
- „Trennen“ in LokyyMail löscht die gespeicherten Zugangsdaten sofort.
