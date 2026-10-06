# 📱 Telegram einrichten

Dauer: ca. 5 Minuten. Pro Kunde ein **eigener Bot**.

> ⚠️ **Nimm niemals den Bot, den Hermes benutzt.** Sonst könnte die KI mitlesen.

## 1. Bot anlegen
1. In Telegram **@BotFather** öffnen → `/newbot` → Namen vergeben → du bekommst einen **Token**.
2. Optional: `/setprivacy` ist nicht nötig, der Bot wird nur im privaten Chat benutzt.

✅ **Fertig, wenn** du einen Token wie `123456:ABC…` hast.

## 2. In Coolify eintragen
Variable `LOKYY_TELEGRAM_BOT_TOKEN` setzen → neu deployen.

✅ **Fertig, wenn** im Log des Dienstes `worker` die Zeile `Telegram aktiv` steht.

## 3. Konto verbinden
LokyyMail → **Konto → Telegram verbinden** → den angezeigten Code als `/start CODE` an den Bot schicken.

✅ **Fertig, wenn** der Bot „Verbunden“ antwortet.

## So benutzt du es
- **Normale Anträge:** Nachricht mit Vorschau, Tipp auf **✅ Freigeben** oder **❌ Ablehnen**.
- **Wichtige Anträge:** Kein Freigeben-Knopf. **Antworte auf die Nachricht** mit deinem 6-stelligen Code aus der Authenticator-App. Deine Code-Nachricht wird danach gelöscht.
- Danach ersetzt der Bot die Vorschau durch das Ergebnis (✅ Ausgeführt, ❌ Abgelehnt, ⌛ Abgelaufen).
- Abends kommt ein Tagesbericht, wenn die KI aufgeräumt hat.

## Gut zu wissen
- Nur **ein Worker** pro Instanz laufen lassen (der Bot fragt Telegram per Long-Polling ab, es muss nichts von außen erreichbar sein).
- Nur verbundene Konten werden bedient. Fremde Klicks werden protokolliert, ohne Inhalt.
- Wer dein Telegram-Konto übernimmt, kann normale Anträge freigeben. Wichtige brauchen zusätzlich deinen Code. Schütze Telegram mit Zwei-Faktor.
- Telegram sieht die Vorschau (Empfänger, Betreff, Textanfang). Wer das nicht will, nutzt nur die Webseite.
