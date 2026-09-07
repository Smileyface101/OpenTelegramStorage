import { useEffect, useState } from 'react'
import { get, post } from '../lib/api'
import { Alert } from '../components/ui'

function Guide({ title, children, open = true }) {
  return (
    <details open={open} className="rounded-lg border border-ink-700 bg-ink-800/40">
      <summary className="cursor-pointer select-none px-3 py-2 text-sm font-medium">{title}</summary>
      <div className="px-3 pb-3 text-sm text-ink-300 space-y-2 [&_ol]:list-decimal [&_ol]:pl-5 [&_ol]:space-y-1 [&_code]:rounded [&_code]:bg-ink-900 [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-ink-200">{children}</div>
    </details>
  )
}

function Field({ label, hint, ...props }) {
  return (
    <div>
      <label className="label">{label}</label>
      <input className="input" {...props} />
      {hint && <p className="text-xs text-ink-400 mt-1">{hint}</p>}
    </div>
  )
}

export default function Setup({ setup, user, onDone, onAdminCreated }) {
  const [step, setStep] = useState(setup.needs_admin ? 1 : (!setup.telegram_connected ? 2 : 3))
  if (!setup.needs_admin && user?.role !== 'admin') {
    return <div className="min-h-screen grid place-items-center text-sm text-ink-400">Only an admin can run setup.</div>
  }
  return (
    <div className="min-h-screen flex items-center justify-center p-4">
      <div className="w-full max-w-lg space-y-4">
        <h1 className="text-xl font-semibold">Set up OpenTelegramStorage</h1>
        <ol className="flex gap-2 text-xs text-ink-400">
          {['Admin account', 'Connect bot', 'Pick channel'].map((l, i) => (
            <li key={l} className={`rounded-full px-3 py-1 ${step === i + 1 ? 'bg-brand-500 text-white' : 'bg-ink-800'}`}>{i + 1}. {l}</li>
          ))}
        </ol>
        {step === 1 && <AdminStep onNext={async () => { await onAdminCreated(); setStep(2) }} />}
        {step === 2 && <BotStep onNext={() => setStep(3)} onSkip={onDone} />}
        {step === 3 && <ChannelStep onDone={onDone} onBack={() => setStep(2)} />}
      </div>
    </div>
  )
}

function AdminStep({ onNext }) {
  const [form, setForm] = useState({ username: 'admin', password: '', confirm: '' })
  const [error, setError] = useState('')
  const submit = async (e) => {
    e.preventDefault(); setError('')
    if (form.password !== form.confirm) return setError('Passwords do not match')
    try { await post('/api/setup/admin', { username: form.username, password: form.password }); onNext() } catch (err) { setError(err.message) }
  }
  return (
    <form onSubmit={submit} className="card space-y-3">
      <p className="text-sm text-ink-300">Create the administrator account for this installation.</p>
      <Guide title="What this account is for" open={false}>
        <p>This is a local login for the web interface only. It has nothing to do with your Telegram account. Anyone who can reach this page and knows the password can see and download every file in your channel, so pick a long passphrase and keep it in a password manager.</p>
        <p>You can add more users later in Settings. Each user gets their own private folder tree inside the same channel.</p>
      </Guide>
      <div><label className="label">Username</label><input className="input" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} /></div>
      <div><label className="label">Password (min 10 chars)</label><input className="input" type="password" autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></div>
      <div><label className="label">Confirm password</label><input className="input" type="password" autoComplete="new-password" value={form.confirm} onChange={(e) => setForm({ ...form, confirm: e.target.value })} /></div>
      <Alert>{error}</Alert>
      <button className="btn-primary">Create account</button>
    </form>
  )
}

export function BotStep({ onNext, onSkip }) {
  const [form, setForm] = useState({ api_id: '', api_hash: '', bot_token: '' })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const submit = async (e) => {
    e.preventDefault(); setError(''); setBusy(true)
    try {
      await post('/api/telegram/connect', { api_id: Number(form.api_id), api_hash: form.api_hash.trim(), bot_token: form.bot_token.trim() })
      onNext()
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }
  return (
    <form onSubmit={submit} className="card space-y-3">
      <p className="text-sm text-ink-300">The app needs its own Telegram bot plus an API key pair. Both take about five minutes and are free. Everything you paste here is encrypted on this server and never sent anywhere except Telegram.</p>
      <Guide title="Step A — create a bot with @BotFather">
        <ol>
          <li>In Telegram, open <a className="underline" href="https://t.me/BotFather" target="_blank" rel="noreferrer">@BotFather</a> and send <code>/newbot</code>.</li>
          <li>Give it a display name (anything, e.g. <i>My File Vault</i>).</li>
          <li>Give it a username ending in <code>bot</code> (e.g. <code>my_file_vault_bot</code>). It must be unused.</li>
          <li>BotFather replies with a token like <code>123456789:AAH…</code>. Copy the whole thing into <b>Bot token</b> below.</li>
        </ol>
        <p>Keep the token secret: whoever has it controls the bot. If it ever leaks, send <code>/revoke</code> to BotFather and paste the new one here.</p>
      </Guide>
      <Guide title="Step B — get an API ID and API hash">
        <ol>
          <li>Open <a className="underline" href="https://my.telegram.org/apps" target="_blank" rel="noreferrer">my.telegram.org/apps</a> and sign in with your phone number. The confirmation code arrives <b>inside Telegram</b> (from the "Telegram" service chat), not by SMS.</li>
          <li>If asked to create an application: any <i>App title</i> and <i>Short name</i> work, choose platform <i>Other</i>, leave URL empty, click <i>Create application</i>.</li>
          <li>Copy <b>App api_id</b> (a number) and <b>App api_hash</b> (32 hex characters) into the fields below.</li>
        </ol>
        <p><b>Why is this needed?</b> Telegram's plain bot API caps uploads at 50 MB. This app talks to Telegram over the same protocol the official apps use (MTProto), which allows 2 GB per file. That protocol requires an API ID and hash; the bot token is what actually logs in, so your personal account is never used.</p>
        <p><b>Trouble?</b> If my.telegram.org shows "ERROR" after the code, wait a few minutes and try again, or use a different browser. Numbers from some VoIP providers are rejected; use the number of a real SIM.</p>
      </Guide>
      <Field label="API ID" hint="Number from my.telegram.org, e.g. 1234567" inputMode="numeric" value={form.api_id} onChange={(e) => setForm({ ...form, api_id: e.target.value })} />
      <Field label="API hash" hint="32 characters from my.telegram.org" value={form.api_hash} onChange={(e) => setForm({ ...form, api_hash: e.target.value })} />
      <Field label="Bot token" hint="From @BotFather, looks like 123456789:AAH…" value={form.bot_token} onChange={(e) => setForm({ ...form, bot_token: e.target.value })} />
      <Alert>{error}</Alert>
      <div className="flex gap-2">
        <button className="btn-primary" disabled={busy}>{busy ? 'Connecting…' : 'Connect bot'}</button>
        {onSkip && <button type="button" className="btn-ghost" onClick={onSkip}>Skip for now</button>}
      </div>
    </form>
  )
}

export function ChannelStep({ onDone, onBack }) {
  const [status, setStatus] = useState(null)
  const [manual, setManual] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const load = async () => setStatus(await get('/api/telegram/status'))
  useEffect(() => { load(); const t = setInterval(load, 3000); return () => clearInterval(t) }, [])
  const choose = async (chatId) => {
    setError(''); setBusy(true)
    try { await post('/api/telegram/channel', { chat_id: Number(chatId) }); onDone() } catch (err) { setError(err.message) } finally { setBusy(false) }
  }
  return (
    <div className="card space-y-3">
      <p className="text-sm text-ink-300">Bot connected as <b>@{status?.bot_username || '…'}</b>. Now it needs a private channel to keep your files in.</p>
      <Guide title="Step C — create the channel and add the bot">
        <ol>
          <li>In Telegram: <i>New Channel</i> (on desktop: menu ☰ → New Channel; on mobile: the pencil button → New Channel). Name it anything, e.g. <i>File Vault</i>.</li>
          <li>Choose <b>Private channel</b>. Skip adding members.</li>
          <li>Open the channel → tap its name → <i>Administrators</i> → <i>Add Administrator</i> → search for <code>@{status?.bot_username || 'your_bot'}</code> and select it.</li>
          <li>Leave the permissions at their defaults; the bot needs at least <b>Post messages</b> and <b>Delete messages</b>. Save.</li>
          <li>Post any message in the channel (a single "hi" is enough). That post is how the bot learns the channel exists; it appears in the list below within a few seconds.</li>
        </ol>
      </Guide>
      <Guide title="Good to know" open={false}>
        <p><b>Why a channel and not a chat?</b> Channel storage is unlimited, messages never expire, and the bot can delete its own posts there, which is how "delete file" really removes data.</p>
        <p><b>Can I use an existing channel?</b> Yes, but every file the app uploads becomes a message in it, so a dedicated one is tidier. Never make the channel public.</p>
        <p><b>Not showing up?</b> Make sure the message was posted <i>after</i> the bot was made admin, then post another one. If it still does not appear, get the channel id by forwarding one of its messages to <a className="underline" href="https://t.me/getidsbot" target="_blank" rel="noreferrer">@getidsbot</a> and enter it manually below (it starts with <code>-100</code>).</p>
      </Guide>
      <div className="space-y-2">
        {(status?.discovered || []).length === 0 && <div className="text-xs text-ink-400">Waiting for a channel post…</div>}
        {(status?.discovered || []).map((c) => (
          <div key={c.chat_id} className="flex items-center justify-between rounded-lg bg-ink-800 px-3 py-2 text-sm">
            <span>{c.title} <span className="text-ink-400 text-xs">{c.chat_id}</span></span>
            <button className="btn-primary" disabled={busy} onClick={() => choose(c.chat_id)}>Use this channel</button>
          </div>
        ))}
      </div>
      <details className="text-sm">
        <summary className="cursor-pointer text-ink-400">Enter the channel id manually</summary>
        <div className="flex gap-2 mt-2">
          <input className="input" placeholder="-1001234567890" value={manual} onChange={(e) => setManual(e.target.value)} />
          <button className="btn-ghost" disabled={busy || !manual} onClick={() => choose(manual)}>Use</button>
        </div>
      </details>
      <Alert>{error}</Alert>
      {status?.channel && <Alert kind="ok">Current channel: {status.channel.title}</Alert>}
      <div className="flex gap-2">
        {onBack && <button className="btn-ghost" onClick={onBack}>Back</button>}
        {status?.channel && <button className="btn-primary" onClick={onDone}>Continue</button>}
      </div>
    </div>
  )
}
