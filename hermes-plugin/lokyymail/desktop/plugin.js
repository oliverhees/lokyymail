// LokyyMail für Hermes Desktop – die Oberfläche für Menschen.
// Die KI schlägt vor (über den MCP-Server), du entscheidest hier.
// Freigeben in Hermes geht nur, wenn der Administrator es eingeschaltet hat (LOKYY_HERMES_APPROVALS) – und immer mit
// deinem 6-stelligen Code. Sonst: Webseite oder Telegram.
//
// Kein Build-Schritt, kein JSX: nur @hermes/plugin-sdk, react und react/jsx-runtime.

import * as sdk from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const { host, useQuery, useQueryClient } = sdk
const ID = 'lokyymail'
const ROUTE = '/lokyymail'

// ------------------------------------------------------------------ kleine Bausteine

const css = {
  page: { height: '100%', overflow: 'auto', padding: '1.25rem 1.5rem', color: 'var(--ui-text-primary)', fontSize: '0.875rem' },
  row: { display: 'flex', gap: '0.5rem', alignItems: 'center', flexWrap: 'wrap' },
  stack: { display: 'flex', flexDirection: 'column', gap: '0.5rem' },
  card: { border: '1px solid var(--ui-stroke-secondary)', borderRadius: '0.6rem', padding: '0.85rem 1rem' },
  high: { borderColor: 'var(--ui-accent)', borderWidth: '2px' },
  muted: { color: 'var(--ui-text-tertiary)' },
  pre: { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', margin: 0, fontFamily: 'inherit', lineHeight: 1.5 },
  input: { padding: '0.45rem 0.6rem', borderRadius: '0.4rem', border: '1px solid var(--ui-stroke-secondary)', background: 'transparent', color: 'inherit', font: 'inherit' },
  tab: (active) => ({ padding: '0.35rem 0.75rem', borderRadius: '0.4rem', border: '1px solid var(--ui-stroke-secondary)', background: active ? 'var(--ui-stroke-secondary)' : 'transparent', color: 'inherit', cursor: 'pointer', font: 'inherit' })
}

function Btn({ children, onClick, disabled, primary, title }) {
  if (sdk.Button) return jsx(sdk.Button, { onClick, disabled, variant: primary ? 'default' : 'outline', size: 'sm', title, children })
  return jsx('button', { type: 'button', onClick, disabled, title, style: { ...css.tab(primary) }, children })
}

const note = (text, extra = {}) => jsx('div', { style: { ...css.muted, ...extra }, children: text })
const errText = (e) => (e && (e.detail || e.message)) || 'Unbekannter Fehler'

// ------------------------------------------------------------------ Einrichtung

function Setup({ ctx, onDone }) {
  const [url, setUrl] = useState('')
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const save = async () => {
    setBusy(true); setError('')
    try {
      await ctx.rest('/config', { method: 'POST', body: { url, token } })
      onDone()
    } catch (e) { setError(errText(e)) } finally { setBusy(false) }
  }
  return jsxs('div', { style: { ...css.stack, maxWidth: '32rem' }, children: [
    jsx('h2', { style: { margin: 0 }, children: 'LokyyMail verbinden' }),
    note('Erstelle auf deiner LokyyMail-Seite unter „Zugänge“ einen Hermes-Desktop-Zugang und trage ihn hier ein.'),
    jsx('input', { style: css.input, placeholder: 'Adresse, z. B. https://mail.firma.de', value: url, onChange: (e) => setUrl(e.target.value), 'aria-label': 'Adresse' }),
    jsx('input', { style: css.input, placeholder: 'Zugang (beginnt mit lkdv_)', value: token, onChange: (e) => setToken(e.target.value), type: 'password', 'aria-label': 'Zugang' }),
    error && jsx('div', { style: { color: 'var(--ui-accent)' }, children: error }),
    jsx('div', { children: jsx(Btn, { primary: true, onClick: save, disabled: busy || !url || !token, children: busy ? 'Prüfe Verbindung…' : 'Verbinden' }) })
  ] })
}

// ------------------------------------------------------------------ Freigaben

function ProposalCard({ ctx, p, baseUrl, hermesApprovals, onChanged }) {
  const [code, setCode] = useState('')
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const pv = p.preview || {}
  const webOnly = !hermesApprovals

  const act = async (path, body) => {
    setBusy(true); setError('')
    try {
      await ctx.rest(`/proposals/${p.id}/${path}`, { method: 'POST', body })
      setAsking(false); setCode(''); onChanged()
    } catch (e) { setError(errText(e)) } finally { setBusy(false) }
  }

  const header = [p.action_label, pv.to && pv.to.length ? `an ${pv.to.join(', ')}` : '', pv.original ? `– ${pv.original.subject || '(ohne Betreff)'}` : '']
    .filter(Boolean).join(' ')

  return jsxs('div', { style: { ...css.card, ...(p.risk_level === 'high' ? css.high : {}), ...css.stack }, children: [
    jsx('strong', { children: header }),
    note(`${pv.mailbox || ''} · ${p.requested_via === 'ai' ? 'von der KI' : 'manuell'} · läuft ab ${new Date(p.expires_at).toLocaleString('de-DE')}`),
    p.risk_reasons && p.risk_reasons.length > 0 && jsxs('div', { children: [
      jsx('strong', { children: 'Hohes Risiko: ' }), p.risk_reasons.join(' · ')
    ] }),
    pv.subject && jsxs('div', { children: [jsx('span', { style: css.muted, children: 'Betreff: ' }), pv.subject] }),
    pv.cc && pv.cc.length > 0 && jsxs('div', { children: [jsx('span', { style: css.muted, children: 'Cc: ' }), pv.cc.join(', ')] }),
    pv.body && jsx('pre', { style: { ...css.pre, ...css.card }, children: pv.body }),
    pv.messages && jsx('div', { children: `${pv.operation}: ${pv.messages.length} Mails – ${pv.messages.slice(0, 5).map((m) => m.subject || '(ohne Betreff)').join(' · ')}` }),
    p.note && note(`Begründung der KI (nicht überprüft): ${p.note}`),
    error && jsx('div', { style: { color: 'var(--ui-accent)' }, children: error }),
    jsxs('div', { style: css.row, children: [
      webOnly
        ? jsx(Btn, { primary: true, onClick: () => ctx.os.openExternal(`${baseUrl}/proposals/${p.id}`), children: 'Auf der Freigabe-Webseite prüfen' })
        : asking
          ? jsxs('div', { style: css.row, children: [
            jsx('input', { style: { ...css.input, width: '7rem', letterSpacing: '0.2em', textAlign: 'center' }, inputMode: 'numeric', autoComplete: 'one-time-code', maxLength: 7, placeholder: 'Code', value: code, onChange: (e) => setCode(e.target.value), 'aria-label': 'Code aus der Authenticator-App' }),
            jsx(Btn, { primary: true, disabled: busy || code.replace(/\D/g, '').length !== 6, onClick: () => act('approve', { code }), children: busy ? 'Wird ausgeführt…' : 'Freigeben' })
          ] })
          : jsx(Btn, { primary: true, onClick: () => setAsking(true), children: 'Freigeben…' }),
      jsx(Btn, { disabled: busy, onClick: () => act('reject', {}), children: 'Ablehnen' })
    ] }),
    webOnly && note('Freigaben in Hermes sind abgeschaltet. Gib auf der Webseite oder per Telegram frei.')
  ] })
}

function Proposals({ ctx, baseUrl, hermesApprovals }) {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: [ID, 'proposals'], queryFn: () => ctx.rest('/proposals'), refetchInterval: 15000 })
  const refresh = () => { qc.invalidateQueries({ queryKey: [ID] }) }
  if (q.isPending) return note('Lade Freigaben…')
  if (q.isError) return note(`Freigaben nicht geladen: ${errText(q.error)}`)
  const items = (q.data && q.data.proposals) || []
  if (!items.length) return note('Nichts zu entscheiden. Sobald deine KI etwas vorschlägt, erscheint es hier.')
  return jsx('div', { style: css.stack, children: items.map((p) => jsx(ProposalCard, { ctx, p, baseUrl, hermesApprovals, onChanged: refresh }, p.id)) })
}

// ------------------------------------------------------------------ Postfach

function MessageView({ ctx, mailbox, messageId, onBack, onProposed }) {
  const q = useQuery({ queryKey: [ID, 'msg', mailbox.id, messageId], queryFn: () => ctx.rest(`/mailboxes/${mailbox.id}/messages/${messageId}`) })
  const [reply, setReply] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const propose = async (action, params) => {
    setBusy(true); setError('')
    try {
      await ctx.rest(`/mailboxes/${mailbox.id}/proposals`, { method: 'POST', body: { action, params: { message_id: messageId, ...params } } })
      onProposed()
    } catch (e) { setError(errText(e)) } finally { setBusy(false) }
  }
  if (q.isPending) return note('Lade Mail…')
  if (q.isError) return note(`Mail nicht geladen: ${errText(q.error)}`)
  const m = q.data
  const inTrash = (m.labels || []).includes('TRASH')
  return jsxs('div', { style: css.stack, children: [
    jsx('div', { children: jsx(Btn, { onClick: onBack, children: 'Zurück' }) }),
    jsx('h3', { style: { margin: 0 }, children: m.subject || '(ohne Betreff)' }),
    note(`Von ${m.from} · ${m.date}`),
    ...(m.warnings || []).map((w, i) => jsx('div', { style: { ...css.card, ...css.high }, children: `Achtung: ${w}` }, `w${i}`)),
    m.attachments && m.attachments.length > 0 && note(`Anhänge: ${m.attachments.map((a) => a.name).join(', ')}`),
    jsx('pre', { style: { ...css.pre, ...css.card }, children: m.text || '(kein Text)' }),
    error && jsx('div', { style: { color: 'var(--ui-accent)' }, children: error }),
    jsxs('div', { style: css.row, children: (inTrash
      ? [['untrash', 'Aus dem Papierkorb holen']]
      : [['archive', 'Archivieren'], [m.unread ? 'mark_read' : 'mark_unread', m.unread ? 'Gelesen' : 'Ungelesen'], ['spam', 'Spam'], ['trash', 'Papierkorb']]
    ).map(([a, label]) => jsx(Btn, { disabled: busy, onClick: () => propose(a, {}), children: label }, a)) }),
    !mailbox.send_disabled && jsxs('div', { style: css.stack, children: [
      jsx('textarea', { style: { ...css.input, minHeight: '7rem' }, placeholder: 'Antwort schreiben…', value: reply, onChange: (e) => setReply(e.target.value), 'aria-label': 'Antwort' }),
      jsx('div', { children: jsx(Btn, { primary: true, disabled: busy || !reply.trim(), onClick: () => propose('reply', { body: reply }), children: 'Antwort zur Freigabe vorlegen' }) })
    ] }),
    note('Jede Aktion wird zuerst ein Antrag. Er erscheint unter „Freigaben“.')
  ] })
}

function Mailbox({ ctx, mailboxes, onProposed }) {
  const [boxId, setBoxId] = useState(mailboxes[0] && mailboxes[0].id)
  const [search, setSearch] = useState('')
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(null)
  const mailbox = mailboxes.find((m) => m.id === boxId) || mailboxes[0]
  const q = useQuery({
    queryKey: [ID, 'list', mailbox && mailbox.id, query],
    queryFn: () => ctx.rest(`/mailboxes/${mailbox.id}/messages${query ? `?q=${encodeURIComponent(query)}` : ''}`),
    enabled: Boolean(mailbox) && !open,
    refetchInterval: 60000
  })
  if (!mailbox) return note('Noch kein Postfach verbunden. Verbinde es auf der LokyyMail-Webseite unter „Postfächer“.')
  if (open) return jsx(MessageView, { ctx, mailbox, messageId: open, onBack: () => setOpen(null), onProposed: () => { setOpen(null); onProposed() } })
  const items = (q.data && q.data.messages) || []
  return jsxs('div', { style: css.stack, children: [
    jsxs('div', { style: css.row, children: [
      mailboxes.length > 1 && jsx('select', { style: css.input, value: mailbox.id, onChange: (e) => setBoxId(e.target.value), 'aria-label': 'Postfach', children: mailboxes.map((m) => jsx('option', { value: m.id, children: m.address }, m.id)) }),
      jsx('input', { style: { ...css.input, flex: 1, minWidth: '12rem' }, placeholder: 'Suchen, z. B. is:unread', value: search, onChange: (e) => setSearch(e.target.value), onKeyDown: (e) => { if (e.key === 'Enter') setQuery(search) }, 'aria-label': 'Suche' }),
      jsx(Btn, { onClick: () => setQuery(search), children: 'Suchen' })
    ] }),
    q.isPending && note('Lade Mails…'),
    q.isError && note(`Mails nicht geladen: ${errText(q.error)}`),
    !q.isPending && !q.isError && !items.length && note('Keine Mails gefunden.'),
    ...items.map((m) => jsxs('button', {
      type: 'button', onClick: () => setOpen(m.id),
      style: { ...css.card, textAlign: 'left', background: 'transparent', color: 'inherit', cursor: 'pointer', font: 'inherit' },
      children: [
        jsx('div', { style: { fontWeight: m.unread ? 700 : 500 }, children: m.subject || '(ohne Betreff)' }),
        note(`${m.from} · ${(m.snippet || '').slice(0, 110)}`)
      ]
    }, m.id))
  ] })
}

// ------------------------------------------------------------------ Seite

function LokyyPage({ ctx }) {
  const qc = useQueryClient()
  const [tab, setTab] = useState('proposals')
  const config = useQuery({ queryKey: [ID, 'config'], queryFn: () => ctx.rest('/config') })
  const status = useQuery({ queryKey: [ID, 'status'], queryFn: () => ctx.rest('/status'), enabled: Boolean(config.data && config.data.configured), refetchInterval: 30000 })
  const refresh = () => qc.invalidateQueries({ queryKey: [ID] })

  let body
  if (config.isPending) body = note('Lade…')
  else if (config.isError) body = note(`Das LokyyMail-Backend in Hermes antwortet nicht (${errText(config.error)}). Ist „lokyymail“ in plugins.enabled eingetragen?`)
  else if (!config.data.configured) body = jsx(Setup, { ctx, onDone: refresh })
  else if (status.isError) body = note(`Keine Verbindung zu LokyyMail: ${errText(status.error)}`)
  else if (status.isPending) body = note('Verbinde…')
  else {
    const pending = status.data.pending || 0
    body = jsxs('div', { style: css.stack, children: [
      jsxs('div', { style: css.row, children: [
        jsx('button', { type: 'button', style: css.tab(tab === 'proposals'), onClick: () => setTab('proposals'), children: `Freigaben${pending ? ` (${pending})` : ''}` }),
        jsx('button', { type: 'button', style: css.tab(tab === 'mail'), onClick: () => setTab('mail'), children: 'Postfach' }),
        jsx('span', { style: { ...css.muted, marginLeft: 'auto' }, children: status.data.user.email })
      ] }),
      tab === 'proposals'
        ? jsx(Proposals, { ctx, baseUrl: config.data.url, hermesApprovals: Boolean(status.data.approvals && status.data.approvals.hermes) })
        : jsx(Mailbox, { ctx, mailboxes: status.data.mailboxes || [], onProposed: () => { refresh(); setTab('proposals') } })
    ] })
  }
  return jsxs('main', { style: css.page, children: [
    jsx('h1', { style: { margin: '0 0 1rem', fontSize: '1.25rem' }, children: 'LokyyMail' }),
    body
  ] })
}

function PendingChip({ ctx }) {
  const config = useQuery({ queryKey: [ID, 'config'], queryFn: () => ctx.rest('/config') })
  const status = useQuery({ queryKey: [ID, 'status'], queryFn: () => ctx.rest('/status'), enabled: Boolean(config.data && config.data.configured), refetchInterval: 30000 })
  const n = (status.data && status.data.pending) || 0
  if (!n) return null
  return jsx('button', {
    type: 'button', onClick: () => host.navigate(ROUTE), title: 'Offene Freigaben in LokyyMail',
    style: { padding: '0 0.4rem', fontSize: '0.6875rem', color: 'var(--ui-accent)', background: 'transparent', border: 0, cursor: 'pointer' },
    children: `✉ ${n} ${n === 1 ? 'Freigabe' : 'Freigaben'}`
  })
}

export default {
  id: ID,
  name: 'LokyyMail',
  defaultEnabled: false,
  register(ctx) {
    ctx.register({ id: 'page', area: sdk.ROUTES_AREA, data: { path: ROUTE }, render: () => jsx(LokyyPage, { ctx }) })
    ctx.register({ id: 'nav', area: sdk.SIDEBAR_NAV_AREA, data: { path: ROUTE, label: 'LokyyMail', codicon: 'mail' } })
    if (sdk.PALETTE_AREA) {
      ctx.register({ id: 'open', area: sdk.PALETTE_AREA, data: { id: 'lokyymail.open', label: 'LokyyMail öffnen', keywords: ['mail', 'freigabe'], run: () => host.navigate(ROUTE) } })
    }
    if (sdk.STATUSBAR_AREAS && sdk.STATUSBAR_AREAS.right) {
      ctx.register({ id: 'pending', area: sdk.STATUSBAR_AREAS.right, order: 140, render: () => jsx(PendingChip, { ctx }) })
    }
  }
}
