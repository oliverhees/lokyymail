# 🛡️ Sicherheitsmodell

## Die Schichten

| # | Schicht | Was sie verhindert |
|---|---|---|
| 1 | KI-Werkzeuge können nur lesen und vorschlagen | KI führt selbst etwas aus |
| 2 | Freigabe nur durch Menschen mit Freigaberecht | Fremde oder die KI geben frei |
| 3 | Zwei-Faktor bei Login und bei hohem Risiko | Gestohlenes Passwort reicht nicht |
| 4 | Freigabe nur über Kanäle, die die KI nicht lesen kann (Webseite, Telegram). Hermes-Karte standardmäßig aus | Agent manipuliert oder belauscht die Oberfläche |
| 5 | Antrag gilt einmal, ist an exakten Inhalt gebunden (Hash) | Nachträgliche Änderung, Doppelausführung |
| 6 | Erneute Prüfung vor dem Ausführen | Mail hat sich inzwischen verändert |
| 7 | Nachprüfung nach dem Ausführen | Stille Fehlschläge |
| 8 | „Senden komplett aus“ pro Postfach | Senden überhaupt |
| 9 | Versteckter Text raus, Injection-Erkennung, Fremd-Hülle | Präparierte Mails steuern die KI |
| 10 | Getrennte Schlüssel (KI ≠ Hermes-Oberfläche), Prüfung schon auf HTTP-Ebene | Schlüssel-Missbrauch |
| 11 | Bremse: max. Anträge pro Stunde und Postfach | Flut von Anträgen |
| 12 | Protokoll ohne Inhalte, Inhalte nach Abschluss gelöscht | Datenabfluss über Logs |

## ⚠️ Ehrliche Grenzen

- **Wer den Server hat, hat alles.** Kommt Hermes an Coolify, Docker, die Datenbank oder `LOKYY_MASTER_KEY`,
  kann er Google-Zugänge und sogar Zwei-Faktor-Geheimnisse entschlüsseln. Deshalb: Hermes ohne Zugang zum
  LokyyMail-Server betreiben, am besten auf einer anderen Maschine.
- **Hermes-Karte:** Der Code läuft durch das Plugin-Backend im Hermes-Gateway, also dort, wo auch die KI arbeitet. Eine
  gekaperte KI mit Zugriff auf diesen Prozess könnte den Code mitlesen und für einen anderen Antrag nutzen. Darum ist die
  Hermes-Freigabe **standardmäßig aus**. Sinnvoll nur, wenn Hermes getrennt läuft und keine Shell-Werkzeuge hat. Der Code gilt
  einmal und kurz, ein Fehlschlag („Code schon benutzt“) fällt dir auf. Besser: Telegram oder Webseite.
- **Aufräumen automatisch:** Eine präparierte Mail kann die KI dazu bringen, Wichtiges zu archivieren. Das ist rückgängig
  machbar und begrenzt (Stundenlimit, Bericht), aber du könntest es übersehen. Die Rückgängig-Liste speichert 7 Tage Absender
  und Betreff.
- **Telegram-Konto:** Wer es übernimmt, kann normale Anträge freigeben. Wichtige brauchen zusätzlich den Code.
- **Injection-Erkennung ist eine Heuristik**, kein Garant. Die harte Grenze ist die Freigabe.
- **Datenabfluss über andere Kanäle** (Web-Anfragen, andere Werkzeuge von Hermes) verhindert LokyyMail nicht.
- **KI-Modell und DSGVO:** Nutzt Hermes ein Cloud-Modell, gehen gelesene Mails an diesen Anbieter.
  Dafür braucht der Kunde einen Auftragsverarbeitungsvertrag. Alternative: lokales Modell.
- `gmail.modify` erlaubt bei Google technisch auch Senden. Der Schutz kommt aus LokyyMail.
