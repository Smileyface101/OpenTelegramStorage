import { useEffect, useState } from 'react'
import { get, post, put, del } from '../lib/api'
import { bytes, when } from '../lib/format'
import { Alert } from '../components/ui'
import { BotStep, ChannelStep } from './Setup'

export default function Settings({ user, onChange }) {
  const isAdmin = user?.role === 'admin'
  return (
    <div className="space-y-6 max-w-2xl">
      {isAdmin && <SystemSection onChange={onChange} />}
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
    try { setS(await put('/api/admin/settings', { part_size_mb: Number(s.part_size_mb), max_retries: Number(s.max_retries), upload_connections: Number(s.upload_connections), stale_upload_hours: Number(s.stale_upload_hours) })); setMsg('Saved') } catch (err) { setError(err.message) }
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
        <div><label className="label">Remove abandoned uploads after (hours, 0 = never)</label>
          <input className="input" type="number" min="0" value={s.stale_upload_hours} onChange={(e) => setS({ ...s, stale_upload_hours: e.target.value })} />
          <p className="text-xs text-ink-400 mt-1">An upload nobody resumed for this long is deleted: its staging files, its rows, and any parts that already reached the channel. Runs every 10 minutes.</p></div>
        <Alert>{error}</Alert><Alert kind="ok">{msg}</Alert>
        <button className="btn-primary">Save</button>
      </form>
    </Section>
  )
}

function Stat({ label, value, sub, warn }) {
  return (
    <div className={`rounded-lg border p-3 ${warn ? 'border-amber-500/40 bg-amber-500/5' : 'border-ink-800 bg-ink-950/40'}`}>
      <div className="text-[11px] uppercase tracking-wide text-ink-400">{label}</div>
      <div className="text-sm font-medium mt-0.5">{value}</div>
      {sub && <div className="text-xs text-ink-400 mt-0.5">{sub}</div>}
    </div>
  )
}

function SystemSection({ onChange }) {
  const [st, setSt] = useState(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const load = async () => { try { setSt(await get('/api/admin/status')) } catch (e) { setError(e.message) } }
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t) }, [])
  const act = async (label, fn) => { setBusy(label); setError(''); try { await fn(); await load(); onChange?.() } catch (e) { setError(e.message) } finally { setBusy('') } }
  if (!st) return <Section title="System">{error ? <Alert>{error}</Alert> : <div className="text-sm text-ink-400">Loading…</div>}</Section>
  const tg = st.telegram; const w = st.worker; const sg = st.staging
  const inFlight = Object.entries(w.in_flight || {})
  const pct = sg.total_bytes ? Math.round((sg.total_bytes - sg.free_bytes) / sg.total_bytes * 100) : 0
  const uptime = st.uptime_seconds; const up = uptime >= 86400 ? `${Math.floor(uptime / 86400)}d ${Math.floor(uptime % 86400 / 3600)}h` : uptime >= 3600 ? `${Math.floor(uptime / 3600)}h ${Math.floor(uptime % 3600 / 60)}m` : `${Math.floor(uptime / 60)}m`
  return (
    <Section title="System">
      <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
        <Stat label="Telegram" warn={!tg.connected || !tg.channel} value={tg.connected ? `connected as @${tg.bot_username}` : (tg.configured ? 'offline' : 'not set up')} sub={tg.channel ? tg.channel.title : 'no channel'} />
        <Stat label="Worker" warn={!w.alive} value={w.alive ? 'running' : 'stopped'} sub={inFlight.length ? `sending part ${inFlight[0][1].part_index + 1}` : 'idle'} />
        <Stat label="Queue" value={`${w.files_by_status.receiving + w.files_by_status.queued + w.files_by_status.uploading} waiting`} sub={`${w.files_by_status.failed} failed · ${w.files_by_status.ready} ready`} warn={w.files_by_status.failed > 0} />
        <Stat label="In channel" value={bytes(w.bytes_in_channel)} sub={`${w.files_by_status.ready} files`} />
        <Stat label="Staging" warn={sg.free_bytes < 2 * 1024 ** 3} value={`${bytes(sg.used_bytes)} in ${sg.file_count} file${sg.file_count === 1 ? '' : 's'}`} sub={`${bytes(sg.free_bytes)} free of ${bytes(sg.total_bytes)} (${pct}% used)`} />
        <Stat label="App" value={`v${st.version} · up ${up}`} sub={`db ${bytes(st.database.size_bytes)} · ${st.uploads.active_sessions} open upload${st.uploads.active_sessions === 1 ? '' : 's'}`} />
      </div>
      {tg.error && <Alert>Telegram: {tg.error}</Alert>}
      <div className="flex flex-wrap gap-2">
        {tg.configured && <button className="btn-ghost" disabled={!!busy} onClick={() => act('tg', () => post('/api/telegram/reconnect'))}>{busy === 'tg' ? 'Reconnecting…' : 'Reconnect Telegram'}</button>}
        <button className="btn-ghost" disabled={!!busy} onClick={() => act('clean', () => post('/api/admin/maintenance/cleanup'))}>{busy === 'clean' ? 'Cleaning…' : 'Run cleanup now'}</button>
        <span className="text-xs text-ink-400 self-center">{st.last_cleanup ? `Last cleanup ${when(st.last_cleanup.at)}: ${st.last_cleanup.stale_uploads + st.last_cleanup.stale_bundles + st.last_cleanup.stale_receiving_files} stale, ${st.last_cleanup.orphan_files_removed} orphan file(s), ${bytes(st.last_cleanup.orphan_bytes_freed)} freed` : 'Cleanup has not run yet'}</span>
      </div>
      {st.import.enabled ? <p className="text-xs text-ink-400">Import directory: {st.import.dir}</p> : <p className="text-xs text-ink-400">No import directory mounted ({st.import.dir}).</p>}
      <details>
        <summary className="cursor-pointer text-sm text-ink-300">Recent warnings and errors ({st.recent_errors.length})</summary>
        {st.recent_errors.length === 0 ? <div className="text-xs text-ink-400 mt-2">None since start.</div> : (
          <ul className="mt-2 max-h-56 overflow-auto rounded-lg bg-ink-950 p-2 text-xs space-y-1">
            {st.recent_errors.map((e, i) => <li key={i} className="font-mono"><span className="text-ink-500">{when(e.at)}</span> <span className={e.level === 'ERROR' ? 'text-red-300' : 'text-amber-300'}>{e.level}</span> <span className="text-ink-400">{e.logger}</span> {e.message}</li>)}
          </ul>
        )}
      </details>
      <Alert>{error}</Alert>
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
