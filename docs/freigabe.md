# 🚦 Wie Freigaben funktionieren

Drei Stufen. Je riskanter, desto strenger.

| Stufe | Was | Bestätigung |
|---|---|---|
| 🟢 **Aufräumen** | archivieren, gelesen, Labels, Spam, wiederherstellen | **automatisch**, mit Bericht und Rückgängig |
| 🟡 **Normal** | Antwort an Kollegen (Firmen-Domain, ohne Anhang) | **1 Tipp**, kein Code |
| 🔴 **Wichtig** | externe Empfänger, Anhang, Weiterleitung, Papierkorb-Sammelaktion, auffällige Mail | **2FA-Code** |

Merksatz: **Nichts Unumkehrbares ohne einen Menschen.**

## 🟢 Aufräumen automatisch

- Pro Postfach einschalten: **Postfächer → Aufräumen automatisch erlauben**. Neue Postfächer starten **aus**.
- Die KI darf dann aufräumen, ohne zu fragen. **Papierkorb, Senden und Weiterleiten sind nie dabei.**
- Bremse: höchstens `LOKYY_AUTO_CLEANUP_PER_HOUR` Mails pro Stunde und Postfach (Standard 60). Danach wieder mit Freigabe.
- Alles steht unter **Aufgeräumt** (Absender und Betreff, `LOKYY_UNDO_DAYS` Tage) mit Knopf **Rückgängig**.
- Tagesbericht per Telegram ab `LOKYY_DAILY_REPORT_HOUR`, wenn etwas aufgeräumt wurde.
- Wenn **du selbst** auf der Webseite auf „Archivieren“ oder „Papierkorb“ klickst, passiert es sofort. Dein Klick ist die Freigabe.

⚠️ Eine präparierte Mail könnte die KI dazu bringen, Wichtiges zu archivieren. Schaden entsteht nicht (Rückgängig), aber du könntest etwas übersehen. Dafür gibt es Bericht und Tagesbericht.

## 📲 Wege zum Bestätigen

| Weg | 🟡 Normal | 🔴 Wichtig |
|---|---|---|
| 🌐 Webseite | Klick | Klick + Code |
| 📱 Telegram | Tipp auf „Freigeben“ | **Antwort auf die Nachricht mit dem Code** |
| 🖥️ Hermes-Karte | Code | Code |

**Warum Telegram sicher ist:** Die KI kann Telegram nicht lesen. Die Vorschau kommt von LokyyMail, nicht von der KI. Ein Code gilt nur für den Antrag, auf den du antwortest, und wird danach aus dem Chat gelöscht.

**Hermes-Karte:** standardmäßig **aus**. Gründe in [sicherheit.md](sicherheit.md). Einschalten mit `LOKYY_HERMES_APPROVALS=1`.

## Was wann „hohes Risiko“ ist

Externer Empfänger · Weiterleitung · Anhang · mehr als 10 Empfänger · Sammelaktion über mehr als 5 Mails (außer Aufräumen) ·
die Mail enthielt versteckten Text oder Anweisungen an die KI.

Interne Firmen-Domains stellst du mit `LOKYY_ORG_DOMAINS` ein. Ohne diese Einstellung gilt die Domain des Postfachs.
