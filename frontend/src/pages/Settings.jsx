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
      {isAdmin && <RecoverySection />}
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
    try { setS(await put('/api/admin/settings', { part_size_mb: Number(s.part_size_mb), max_retries: Number(s.max_retries), compress_archives: s.compress_archives, upload_connections: Number(s.upload_connections) })); setMsg('Saved') } catch (err) { setError(err.message) }
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
        <div><label className="label">Parallel upload connections (1–16)</label>
          <input className="input" type="number" min="1" max="16" value={s.upload_connections} onChange={(e) => setS({ ...s, upload_connections: e.target.value })} />
          <p className="text-xs text-ink-400 mt-1">Files over 10 MB are pushed to Telegram over this many connections at once. 4 is a good default; 1 uses the classic single-connection uploader.</p></div>
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

function RecoverySection() {
  const [st, setSt] = useState(null)
  const [error, setError] = useState('')
  const load = async () => { try { setSt(await get('/api/admin/rebuild')) } catch { /* ignore */ } }
  useEffect(() => { load() }, [])
  useEffect(() => { if (!st?.running) return; const t = setInterval(load, 1500); return () => clearInterval(t) }, [st?.running])
  const start = async () => {
    setError('')
    if (!confirm('Scan the whole channel and import every file that is not in the index? Existing entries are left untouched.')) return
    try { setSt(await post('/api/admin/rebuild')) } catch (e) { setError(e.message) }
  }
  return (
    <Section title="Recovery">
      <p className="text-sm text-ink-300">Every part the app posts carries a small JSON caption, so the channel alone is enough to rebuild the file list. Use this after restoring the app on a new machine, after losing the data volume, or if the index and the channel ever disagree.</p>
      <div className="flex items-center gap-3">
        <button className="btn-ghost" onClick={start} disabled={st?.running}>{st?.running ? 'Scanning…' : 'Rebuild index from channel'}</button>
        {st?.running && <span className="text-xs text-ink-400">scanned {st.scanned} / {st.last_message_id} messages · {st.parts_found} parts found</span>}
      </div>
      {st && !st.running && st.finished_at && (
        <div className="text-sm text-ink-300">
          Last run: <b>{st.files_imported}</b> imported, <b>{st.files_skipped}</b> already indexed, <b>{st.files_incomplete}</b> with missing parts.
          {st.error && <div className="text-red-300">Error: {st.error}</div>}
        </div>
      )}
      {st?.log?.length > 0 && (
        <pre className="max-h-40 overflow-auto rounded-lg bg-ink-950 p-3 text-xs text-ink-400">{st.log.join('\n')}</pre>
      )}
      <Alert>{error}</Alert>
    </Section>
  )
}
