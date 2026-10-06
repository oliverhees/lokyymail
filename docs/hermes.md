# 🤖 Hermes anbinden

Zwei Teile, beide optional kombinierbar:

| Teil | Wofür | Schlüssel |
|---|---|---|
| **MCP-Server** | Hermes (oder Claude, n8n) liest und schlägt vor | KI-Zugang `lkai_…` |
| **Desktop-Plugin** | Du siehst Postfach und Freigaben in Hermes | Hermes-Desktop-Zugang `lkdv_…` |

Beide Zugänge erstellst du in LokyyMail unter **Zugänge**. Sie werden nur einmal angezeigt.

## 1. MCP-Server

In `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  lokyymail:
    url: "https://mail.kunde.de/mcp"
    headers:
      Authorization: "Bearer ${LOKYYMAIL_AI_KEY}"
    enabled: true
    timeout: 120
    tools:
      resources: false
      prompts: false
```

Den Schlüssel **nicht** in die Datei schreiben, sondern in `~/.hermes/.env`:
`LOKYYMAIL_AI_KEY=lkai_…`

Über **MetaMCP**: als Streamable-HTTP-Server mit derselben URL und demselben Header eintragen.

✅ **Fertig, wenn** Hermes auf „Welche Postfächer hast du?“ mit `list_mailboxes` antwortet.

**Wichtig:** Ein anderes Gmail-Werkzeug in Hermes (mit eigenem Google-Zugang) abschalten.
Sonst kann Hermes LokyyMail umgehen.

## 2. Prompt für Hermes

Den Inhalt von [`hermes/prompt.md`](../hermes/prompt.md) in Hermes' Gedächtnis oder als Skill ablegen.

## 3. Desktop-Plugin

1. Ordner `hermes-plugin/lokyymail` nach `~/.hermes/plugins/lokyymail/` kopieren.
2. In `~/.hermes/config.yaml` unter `plugins.enabled` den Eintrag `lokyymail` ergänzen. Gateway neu starten.
3. Hermes Desktop → **Capabilities → Plugins** → **Rescan** → **LokyyMail** einschalten.
4. In der Seitenleiste **LokyyMail** öffnen → Adresse und `lkdv_…`-Zugang eintragen.

✅ **Fertig, wenn** unter „Freigaben“ deine offenen Anträge erscheinen.

**So gibst du frei:** Am bequemsten per **Telegram** ([telegram.md](telegram.md)). In der Hermes-Karte geht es nur, wenn der
Administrator `LOKYY_HERMES_APPROVALS=1` gesetzt hat, dann immer mit deinem 6-stelligen Code. Sonst führt der Knopf zur Webseite.
Stufen und Wege: [freigabe.md](freigabe.md).
