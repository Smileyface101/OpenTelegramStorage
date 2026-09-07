import { useEffect, useState } from 'react'
import { get, post } from '../lib/api'
import { Alert } from '../components/ui'

export default function Setup({ setup, user, onDone, onAdminCreated }) {
  const [step, setStep] = useState(setup.needs_admin ? 1 : (!setup.telegram_connected ? 2 : 3))
  if (!setup.needs_admin && user?.role !== 'admin') {
    return <div className="min-h-screen grid place-items-center text-sm text-ink-400">Only an admin can run setup.</div>
  }
  return (
    <div className="min-h-screen flex items-center justify-center p-4">
      <div className="w-full max-w-lg space-y-4">
        <h1 className="text-xl font-semibold">Set up OpenTelegramHosting</h1>
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
      <p className="text-sm text-ink-300">Create the administrator account for this installation. Use a long password; it protects everything in your channel.</p>
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
      <div className="text-sm text-ink-300 space-y-2">
        <p>1. Create a bot with <b>@BotFather</b> and copy its token.</p>
        <p>2. Get an <b>API ID</b> and <b>API hash</b> at <a className="underline" href="https://my.telegram.org/apps" target="_blank" rel="noreferrer">my.telegram.org/apps</a>. They are needed because this app talks to Telegram over MTProto, which allows 2 GB uploads instead of the 50 MB HTTP bot limit.</p>
        <p>Credentials are stored encrypted on this server and never leave it.</p>
      </div>
      <div><label className="label">API ID</label><input className="input" inputMode="numeric" value={form.api_id} onChange={(e) => setForm({ ...form, api_id: e.target.value })} /></div>
      <div><label className="label">API hash</label><input className="input" value={form.api_hash} onChange={(e) => setForm({ ...form, api_hash: e.target.value })} /></div>
      <div><label className="label">Bot token</label><input className="input" value={form.bot_token} onChange={(e) => setForm({ ...form, bot_token: e.target.value })} /></div>
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
      <div className="text-sm text-ink-300 space-y-2">
        <p>Connected as <b>@{status?.bot_username || '…'}</b>.</p>
        <p>1. Create a <b>private channel</b> in Telegram (it will hold your files).</p>
        <p>2. Add the bot as an <b>administrator</b> with permission to post and delete messages.</p>
        <p>3. Post any message in the channel. It appears below within a few seconds.</p>
      </div>
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
