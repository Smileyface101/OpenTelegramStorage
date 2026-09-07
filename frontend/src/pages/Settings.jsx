import { useEffect, useState } from 'react'
import { get, post, put, del } from '../lib/api'
import { Alert } from '../components/ui'
import { BotStep, ChannelStep } from './Setup'

export default function Settings({ user, onChange }) {
  const isAdmin = user?.role === 'admin'
  return (
    <div className="space-y-6 max-w-2xl">
      {isAdmin && <TelegramSection onChange={onChange} />}
      {isAdmin && <TransferSection />}
      <PasswordSection />
      {isAdmin && <UsersSection me={user} />}
    </div>
  )
}

function Section({ title, children }) {
  return <section className="card space-y-3"><h2 className="font-semibold">{title}</h2>{children}</section>
}

function TelegramSection({ onChange }) {
  const [status, setStatus] = useState(null)
  const [mode, setMode] = useState(null) // null | 'bot' | 'channel'
  const load = async () => setStatus(await get('/api/telegram/status'))
  useEffect(() => { load() }, [])
  const disconnect = async () => {
    if (!confirm('Forget the bot credentials and channel? Files stay in the channel; you can reconnect later.')) return
    await post('/api/telegram/disconnect'); await load(); onChange()
  }
  if (!status) return null
  return (
    <Section title="Telegram">
      {status.error && <Alert>Connection error: {status.error}</Alert>}
      <dl className="grid grid-cols-3 gap-y-1 text-sm">
        <dt className="text-ink-400">Bot</dt><dd className="col-span-2">{status.connected ? `@${status.bot_username}` : (status.configured ? 'configured, offline' : 'not connected')}</dd>
        <dt className="text-ink-400">Channel</dt><dd className="col-span-2">{status.channel ? `${status.channel.title} (${status.channel.id})` : 'not selected'}</dd>
      </dl>
      <div className="flex flex-wrap gap-2">
        <button className="btn-ghost" onClick={() => setMode(mode === 'bot' ? null : 'bot')}>{status.configured ? 'Change bot' : 'Connect bot'}</button>
        {status.connected && <button className="btn-ghost" onClick={() => setMode(mode === 'channel' ? null : 'channel')}>Change channel</button>}
        {status.configured && <button className="btn-danger" onClick={disconnect}>Disconnect</button>}
      </div>
      {mode === 'bot' && <BotStep onNext={async () => { setMode('channel'); await load(); onChange() }} />}
      {mode === 'channel' && <ChannelStep onDone={async () => { setMode(null); await load(); onChange() }} />}
    </Section>
  )
}

function TransferSection() {
  const [s, setS] = useState(null)
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')
  useEffect(() => { get('/api/admin/settings').then(setS) }, [])
  const save = async (e) => {
    e.preventDefault(); setMsg(''); setError('')
    try { setS(await put('/api/admin/settings', { part_size_mb: Number(s.part_size_mb), max_retries: Number(s.max_retries), compress_archives: s.compress_archives })); setMsg('Saved') } catch (err) { setError(err.message) }
  }
  if (!s) return null
  return (
    <Section title="Transfers">
      <form onSubmit={save} className="space-y-3">
        <div>
          <label className="label">Part size (MB, max {s.max_part_size_mb})</label>
          <input className="input" type="number" min="1" max={s.max_part_size_mb} value={s.part_size_mb} onChange={(e) => setS({ ...s, part_size_mb: e.target.value })} />
          <p className="text-xs text-ink-400 mt-1">Files larger than this are split into numbered parts, one channel message each. Smaller parts retry faster; larger parts mean fewer messages.</p>
        </div>
        <div><label className="label">Retries before a transfer is marked failed</label>
          <input className="input" type="number" min="0" max="20" value={s.max_retries} onChange={(e) => setS({ ...s, max_retries: e.target.value })} /></div>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={s.compress_archives} onChange={(e) => setS({ ...s, compress_archives: e.target.checked })} /> Compress zip archives by default</label>
        <Alert>{error}</Alert><Alert kind="ok">{msg}</Alert>
        <button className="btn-primary">Save</button>
      </form>
    </Section>
  )
}

function PasswordSection() {
  const [f, setF] = useState({ current_password: '', new_password: '', confirm: '' })
  const [msg, setMsg] = useState(''); const [error, setError] = useState('')
  const submit = async (e) => {
    e.preventDefault(); setMsg(''); setError('')
    if (f.new_password !== f.confirm) return setError('Passwords do not match')
    try { await post('/api/auth/password', { current_password: f.current_password, new_password: f.new_password }); setMsg('Password changed. Other sessions were signed out.'); setF({ current_password: '', new_password: '', confirm: '' }) } catch (err) { setError(err.message) }
  }
  return (
    <Section title="Password">
      <form onSubmit={submit} className="space-y-3">
        <div><label className="label">Current password</label><input className="input" type="password" autoComplete="current-password" value={f.current_password} onChange={(e) => setF({ ...f, current_password: e.target.value })} /></div>
        <div><label className="label">New password</label><input className="input" type="password" autoComplete="new-password" value={f.new_password} onChange={(e) => setF({ ...f, new_password: e.target.value })} /></div>
        <div><label className="label">Confirm</label><input className="input" type="password" autoComplete="new-password" value={f.confirm} onChange={(e) => setF({ ...f, confirm: e.target.value })} /></div>
        <Alert>{error}</Alert><Alert kind="ok">{msg}</Alert>
        <button className="btn-primary">Change password</button>
      </form>
    </Section>
  )
}

function UsersSection({ me }) {
  const [users, setUsers] = useState([])
  const [f, setF] = useState({ username: '', password: '', role: 'user' })
  const [error, setError] = useState('')
  const load = () => get('/api/admin/users').then(setUsers)
  useEffect(() => { load() }, [])
  const add = async (e) => {
    e.preventDefault(); setError('')
    try { await post('/api/admin/users', f); setF({ username: '', password: '', role: 'user' }); load() } catch (err) { setError(err.message) }
  }
  const act = async (fn) => { setError(''); try { await fn(); load() } catch (err) { setError(err.message) } }
  return (
    <Section title="Users">
      <p className="text-xs text-ink-400">Each user has a private file tree inside the same channel.</p>
      <ul className="divide-y divide-ink-800 text-sm">
        {users.map((u) => (
          <li key={u.id} className="py-2 flex items-center justify-between gap-2">
            <span>{u.username} <span className="text-xs text-ink-400">{u.role}{!u.is_active && ' · disabled'}</span></span>
            {u.id !== me.id && (
              <span className="flex gap-2">
                <button className="btn-ghost" onClick={() => act(() => post(`/api/admin/users/${u.id}/toggle`))}>{u.is_active ? 'Disable' : 'Enable'}</button>
                <button className="btn-danger" onClick={() => confirm(`Delete user ${u.username}?`) && act(() => del(`/api/admin/users/${u.id}`))}>Delete</button>
              </span>
            )}
          </li>
        ))}
      </ul>
      <form onSubmit={add} className="grid grid-cols-1 sm:grid-cols-4 gap-2">
        <input className="input" placeholder="username" value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} />
        <input className="input" type="password" placeholder="password" autoComplete="new-password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} />
        <select className="input" value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}><option value="user">user</option><option value="admin">admin</option></select>
        <button className="btn-primary justify-center">Add user</button>
      </form>
      <Alert>{error}</Alert>
    </Section>
  )
}
