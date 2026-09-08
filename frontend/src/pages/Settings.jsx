import { useEffect, useState } from 'react'
import { get, post, put, del } from '../lib/api'
import { bytes, when } from '../lib/format'
import { Alert } from '../components/ui'
import { BotStep, ChannelStep } from './Setup'
import QRCode from 'qrcode'

export default function Settings({ user, onChange }) {
  const isAdmin = user?.role === 'admin'
  return (
    <div className="space-y-6 max-w-2xl">
      {isAdmin && <SystemSection onChange={onChange} />}
      {isAdmin && <TelegramSection onChange={onChange} />}
      {isAdmin && <WorkspaceSection />}
      {isAdmin && <TransferSection />}
      {isAdmin && <EncryptionSection />}
      {isAdmin && <RecoverySection />}
      <SharesSection />
      <PasswordSection />
      <TwoFactorSection />
      <SessionsSection />
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
  const [creds, setCreds] = useState(null)
  const load = async () => setStatus(await get('/api/telegram/status'))
  const reveal = async () => {
    const password = prompt('Enter your password to show the Telegram credentials:')
    if (!password) return
    try { setCreds(await post('/api/telegram/reveal', { password })) } catch (e) { alert(e.message) }
  }
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
        {status.channel && <><dt className="text-ink-400">Auto-delete</dt><dd className={`col-span-2 ${status.auto_delete_seconds ? 'text-red-300 font-medium' : 'text-emerald-300'}`}>{status.auto_delete_seconds ? `ON (${Math.round(status.auto_delete_seconds / 86400) || 1} day(s)) — turn it off in Telegram, files are being destroyed` : 'off'}</dd></>}
      </dl>
      <div className="flex flex-wrap gap-2">
        <button className="btn-ghost" onClick={() => setMode(mode === 'bot' ? null : 'bot')}>{status.configured ? 'Change bot' : 'Connect bot'}</button>
        {status.connected && <button className="btn-ghost" onClick={() => setMode(mode === 'channel' ? null : 'channel')}>Change channel</button>}
        {status.configured && <button className="btn-ghost" onClick={() => (creds ? setCreds(null) : reveal())}>{creds ? 'Hide credentials' : 'Show credentials'}</button>}
        {status.configured && <button className="btn-danger" onClick={disconnect}>Disconnect</button>}
      </div>
      {creds && (
        <div className="rounded-lg border border-ink-700 p-3 text-sm space-y-1">
          <div className="text-xs text-ink-400">For setting up another server on the same channel. Keep these secret.</div>
          <div className="grid grid-cols-[110px_1fr] gap-y-1 font-mono text-xs">
            <span className="text-ink-400 font-sans">API id</span><code className="select-all">{creds.api_id}</code>
            <span className="text-ink-400 font-sans">API hash</span><code className="select-all break-all">{creds.api_hash}</code>
            <span className="text-ink-400 font-sans">Bot token</span><code className="select-all break-all">{creds.bot_token}</code>
            <span className="text-ink-400 font-sans">Channel</span><code className="select-all">{creds.channel_title} ({creds.channel_id})</code>
          </div>
        </div>
      )}
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
    try { setS(await put('/api/admin/settings', { part_size_mb: Number(s.part_size_mb), max_retries: Number(s.max_retries), upload_connections: Number(s.upload_connections), stale_upload_hours: Number(s.stale_upload_hours), public_url: s.public_url ?? '' })); setMsg('Saved') } catch (err) { setError(err.message) }
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
        <div><label className="label">Public URL for share links (optional)</label>
          <input className="input" placeholder="https://files.example.com" value={s.public_url || ''} onChange={(e) => setS({ ...s, public_url: e.target.value })} />
          <p className="text-xs text-ink-400 mt-1">Used to build share links. Leave empty to use whatever address you open the app with.</p></div>
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
      {st.sync?.mode === 'shared' && (
        <div className="rounded-lg border border-ink-800 bg-ink-950/40 p-3 text-xs space-y-1">
          <div className="text-[11px] uppercase tracking-wide text-ink-400">Shared workspace sync · server "{st.sync.name}" · id {st.sync.server_id || '…'}</div>
          <div>Last live message: {st.sync.last_live_at ? `${when(st.sync.last_live_at)} (${st.sync.last_live_kind})` : 'none yet'} · parts indexed {st.sync.parts_indexed} · events applied {st.sync.events_applied}</div>
          <div>Last catch-up: {st.sync.last_catchup_at ? `${when(st.sync.last_catchup_at)} — scanned ${st.sync.last_catchup?.scanned ?? 0}, ${st.sync.last_catchup?.parts ?? 0} part(s), ${st.sync.last_catchup?.events ?? 0} event(s)` : 'not yet'} · up to message #{st.sync.last_message_id}</div>
          {st.sync.last_error && <div className="text-red-300">Last sync error: {st.sync.last_error}</div>}
        </div>
      )}
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
            <span>{u.username} <span className="text-xs text-ink-400">{u.role}{u.totp_enabled && ' · 2FA'}{!u.is_active && ' · disabled'}</span></span>
            {u.id !== me.id && (
              <span className="flex gap-2">
                {u.totp_enabled && <button className="btn-ghost" title="Switch off this user's two-factor (lost phone) and sign them out everywhere" onClick={() => confirm(`Reset two-factor for ${u.username}? They will be signed out everywhere.`) && act(() => post(`/api/admin/users/${u.id}/totp/reset`))}>Reset 2FA</button>}
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

function TwoFactorSection() {
  const [st, setSt] = useState(null)
  const [setup, setSetup] = useState(null)      // { secret, uri, qr }
  const [code, setCode] = useState('')
  const [codes, setCodes] = useState(null)      // freshly issued recovery codes
  const [disable, setDisable] = useState(null)  // { password, code }
  const [error, setError] = useState('')
  const [msg, setMsg] = useState('')
  const load = () => get('/api/auth/totp').then(setSt).catch(() => {})
  useEffect(() => { load() }, [])
  const begin = async () => {
    setError(''); setMsg(''); setCodes(null)
    try { const r = await post('/api/auth/totp/setup'); const qr = await QRCode.toDataURL(r.uri, { margin: 1, width: 200 }); setSetup({ ...r, qr }) } catch (e) { setError(e.message) }
  }
  const enable = async (e) => {
    e.preventDefault(); setError('')
    try { const r = await post('/api/auth/totp/enable', { code: code.trim() }); setCodes(r.recovery_codes); setSetup(null); setCode(''); setMsg('Two-factor is on. Other sessions were signed out.'); load() } catch (err) { setError(err.message) }
  }
  const regen = async () => {
    const c = prompt('Enter a current authenticator code to issue new recovery codes (old ones stop working):')
    if (!c) return
    setError('')
    try { const r = await post('/api/auth/totp/recovery-codes', { code: c.trim() }); setCodes(r.recovery_codes); load() } catch (err) { setError(err.message) }
  }
  const doDisable = async (e) => {
    e.preventDefault(); setError('')
    try { await post('/api/auth/totp/disable', disable); setDisable(null); setCodes(null); setMsg('Two-factor is off.'); load() } catch (err) { setError(err.message) }
  }
  if (!st) return null
  return (
    <Section title="Two-factor authentication">
      <p className="text-sm text-ink-300">{st.enabled ? <>Enabled. Signing in needs your password plus a code from your authenticator app. <b>{st.recovery_codes_left}</b> recovery codes left.</> : 'Off. Add a second step to sign-in using any authenticator app (Aegis, Google Authenticator, 1Password, Bitwarden…).'}</p>
      {!st.enabled && !setup && <button className="btn-primary" onClick={begin}>Set up two-factor</button>}
      {setup && (
        <form onSubmit={enable} className="space-y-3">
          <div className="flex flex-col sm:flex-row gap-4 items-start">
            <img src={setup.qr} alt="QR code" className="rounded-lg bg-white p-1 w-[200px] h-[200px]" />
            <div className="text-sm text-ink-300 space-y-2">
              <p>1. Scan this with your authenticator app.</p>
              <p>2. Or enter the key by hand: <code className="bg-ink-800 px-1 rounded break-all">{setup.secret}</code></p>
              <p>3. Type the 6-digit code the app shows to confirm.</p>
            </div>
          </div>
          <div className="flex gap-2"><input className="input max-w-[200px] tracking-widest" inputMode="numeric" placeholder="123456" value={code} onChange={(e) => setCode(e.target.value)} /><button className="btn-primary" disabled={code.length < 6}>Turn on</button><button type="button" className="btn-ghost" onClick={() => setSetup(null)}>Cancel</button></div>
        </form>
      )}
      {codes && (
        <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 space-y-2">
          <div className="text-sm font-medium text-amber-200">Recovery codes — save these now, they are shown once</div>
          <p className="text-xs text-ink-300">Each code signs you in once if you lose your authenticator. Keep them somewhere safe, not in this browser.</p>
          <pre className="grid grid-cols-2 gap-x-6 text-sm font-mono">{codes.map((c) => <span key={c}>{c}</span>)}</pre>
          <button className="btn-ghost" onClick={() => navigator.clipboard?.writeText(codes.join('\n'))}>Copy</button>
        </div>
      )}
      {st.enabled && !disable && (
        <div className="flex flex-wrap gap-2">
          <button className="btn-ghost" onClick={regen}>New recovery codes</button>
          <button className="btn-danger" onClick={() => setDisable({ password: '', code: '' })}>Turn off</button>
        </div>
      )}
      {disable && (
        <form onSubmit={doDisable} className="space-y-2">
          <p className="text-xs text-ink-400">Confirm with your password and a current code (or a recovery code).</p>
          <div className="grid sm:grid-cols-3 gap-2">
            <input className="input" type="password" autoComplete="current-password" placeholder="password" value={disable.password} onChange={(e) => setDisable({ ...disable, password: e.target.value })} />
            <input className="input" placeholder="code" value={disable.code} onChange={(e) => setDisable({ ...disable, code: e.target.value })} />
            <div className="flex gap-2"><button className="btn-danger">Turn off</button><button type="button" className="btn-ghost" onClick={() => setDisable(null)}>Cancel</button></div>
          </div>
        </form>
      )}
      <Alert>{error}</Alert><Alert kind="ok">{msg}</Alert>
    </Section>
  )
}

function SessionsSection() {
  const [list, setList] = useState([])
  const [error, setError] = useState('')
  const load = () => get('/api/auth/sessions').then(setList).catch((e) => setError(e.message))
  useEffect(() => { load() }, [])
  const revoke = async (s) => { setError(''); try { await del(`/api/auth/sessions/${s.id}`); load() } catch (e) { setError(e.message) } }
  const revokeOthers = async () => { if (!confirm('Sign out every other device?')) return; setError(''); try { await post('/api/auth/sessions/revoke-others'); load() } catch (e) { setError(e.message) } }
  const ua = (s) => { const u = s.user_agent || ''; const m = u.match(/(Firefox|Edg|Chrome|Safari)\/[\d.]+/); const os = /Windows/.test(u) ? 'Windows' : /Mac OS/.test(u) ? 'macOS' : /Android/.test(u) ? 'Android' : /iPhone|iPad/.test(u) ? 'iOS' : /Linux/.test(u) ? 'Linux' : ''; return `${m ? m[1].replace('Edg', 'Edge') : (u.slice(0, 24) || 'unknown client')}${os ? ' · ' + os : ''}` }
  return (
    <Section title="Signed-in devices">
      <ul className="divide-y divide-ink-800 text-sm">
        {list.map((s) => (
          <li key={s.id} className="py-2 flex items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="truncate">{ua(s)} {s.current && <span className="ml-1 rounded bg-emerald-500/15 px-1.5 py-0.5 text-xs text-emerald-300">this device</span>}</div>
              <div className="text-xs text-ink-400">{s.ip} · signed in {when(s.created_at)} · last seen {when(s.last_seen_at)}</div>
            </div>
            {!s.current && <button className="btn-ghost" onClick={() => revoke(s)}>Sign out</button>}
          </li>
        ))}
      </ul>
      {list.length > 1 && <button className="btn-danger" onClick={revokeOthers}>Sign out all other devices</button>}
      <Alert>{error}</Alert>
    </Section>
  )
}

function SharesSection() {
  const [list, setList] = useState(null)
  const [error, setError] = useState('')
  const load = () => get('/api/shares').then(setList).catch((e) => setError(e.message))
  useEffect(() => { load() }, [])
  const toggle = async (s) => { try { await post(`/api/shares/${s.id}/toggle`); load() } catch (e) { setError(e.message) } }
  const remove = async (s) => { if (!confirm('Delete this link?')) return; try { await del(`/api/shares/${s.id}`); load() } catch (e) { setError(e.message) } }
  if (list === null) return null
  return (
    <Section title="Share links">
      {list.length === 0 ? <p className="text-sm text-ink-400">No share links. Use the link icon on a file to create one.</p> : (
        <ul className="divide-y divide-ink-800 text-sm">
          {list.map((s) => (
            <li key={s.id} className={`py-2 flex items-center justify-between gap-3 ${s.active ? '' : 'opacity-60'}`}>
              <div className="min-w-0">
                <div className="truncate">{s.file_name}{s.label ? <span className="text-ink-400"> · {s.label}</span> : ''}</div>
                <div className="text-xs text-ink-400 truncate">{s.url}</div>
                <div className="text-xs text-ink-500">{s.download_count} download{s.download_count === 1 ? '' : 's'}{s.max_downloads != null && ` of ${s.max_downloads}`}{s.expires_at && ` · expires ${when(s.expires_at)}`}{s.has_password && ' · password'}{s.disabled && ' · disabled'}{s.last_access_at && ` · last used ${when(s.last_access_at)}`}</div>
              </div>
              <span className="flex gap-2 shrink-0">
                <button className="btn-ghost" onClick={() => navigator.clipboard?.writeText(s.url)}>Copy</button>
                <button className="btn-ghost" onClick={() => toggle(s)}>{s.disabled ? 'Enable' : 'Disable'}</button>
                <button className="btn-danger" onClick={() => remove(s)}>Delete</button>
              </span>
            </li>
          ))}
        </ul>
      )}
      <Alert>{error}</Alert>
    </Section>
  )
}

function EncryptionSection() {
  const [st, setSt] = useState(null)
  const [shown, setShown] = useState(null)   // exported key
  const [imp, setImp] = useState(null)       // { key, password }
  const [error, setError] = useState('')
  const [msg, setMsg] = useState('')
  const load = () => get('/api/admin/encryption').then(setSt).catch((e) => setError(e.message))
  useEffect(() => { load() }, [])
  const toggle = async () => {
    setError(''); setMsg('')
    try { await put('/api/admin/settings', { encrypt_new: !st.encrypt_new }); await load() } catch (e) { setError(e.message) }
  }
  const exportKey = async () => {
    const password = prompt('Enter your password to reveal the content key:')
    if (!password) return
    setError('')
    try { setShown(await post('/api/admin/encryption/export', { password })) } catch (e) { setError(e.message) }
  }
  const doImport = async (e) => {
    e.preventDefault(); setError(''); setMsg('')
    try { const r = await post('/api/admin/encryption/import', imp); setImp(null); setMsg(`Key ${r.key_id} installed.`); load() } catch (err) { setError(err.message) }
  }
  if (!st) return null
  return (
    <Section title="Content encryption">
      <p className="text-sm text-ink-300">
        {st.encrypt_new ? 'On: new uploads are encrypted on this server before they reach Telegram (AES-256-GCM, per-part keys). Telegram only ever holds ciphertext.' : 'Off: new uploads are stored in the channel as-is. Anyone with access to the channel can read them.'}
        {' '}{st.encrypted_files > 0 && <>{st.encrypted_files} encrypted file{st.encrypted_files === 1 ? '' : 's'} so far.</>}
      </p>
      <div className="flex flex-wrap gap-2 items-center">
        <button className={st.encrypt_new ? 'btn-ghost' : 'btn-primary'} onClick={toggle}>{st.encrypt_new ? 'Turn off for new uploads' : 'Turn on'}</button>
        {st.has_key && <button className="btn-ghost" onClick={exportKey}>Export key</button>}
        {!imp && <button className="btn-ghost" onClick={() => setImp({ key: '', password: '' })}>Import key</button>}
        {st.key_id && <span className="text-xs text-ink-400">key id <code className="bg-ink-800 px-1 rounded">{st.key_id}</code></span>}
      </div>
      {st.has_key && (
        <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-100 space-y-1">
          <div className="font-medium">Back up the content key.</div>
          <p className="text-xs">The key lives only in this server's data directory, encrypted with the master key. If the data volume is lost without a copy of the content key, every encrypted file in the channel becomes permanently unreadable. Export it once and keep it with your other secrets. Recovery on a new machine: import the key, then run "Rebuild index from channel".</p>
        </div>
      )}
      {shown && (
        <div className="rounded-lg border border-ink-700 p-3 space-y-2">
          <div className="text-xs uppercase tracking-wide text-ink-400">Content key (hex, key id {shown.key_id})</div>
          <code className="block break-all text-sm select-all">{shown.key}</code>
          <div className="flex gap-2"><button className="btn-ghost" onClick={() => navigator.clipboard?.writeText(shown.key)}>Copy</button><button className="btn-ghost" onClick={() => setShown(null)}>Hide</button></div>
        </div>
      )}
      {imp && (
        <form onSubmit={doImport} className="space-y-2">
          <p className="text-xs text-ink-400">Paste a previously exported key (64 hex characters). Refused while files encrypted with the current key exist.</p>
          <input className="input font-mono" placeholder="content key (hex)" value={imp.key} onChange={(e) => setImp({ ...imp, key: e.target.value.trim() })} />
          <div className="flex gap-2"><input className="input" type="password" placeholder="your password" autoComplete="current-password" value={imp.password} onChange={(e) => setImp({ ...imp, password: e.target.value })} /><button className="btn-primary" disabled={imp.key.length !== 64 || !imp.password}>Install</button><button type="button" className="btn-ghost" onClick={() => setImp(null)}>Cancel</button></div>
        </form>
      )}
      <Alert>{error}</Alert><Alert kind="ok">{msg}</Alert>
    </Section>
  )
}

function WorkspaceSection() {
  const [s, setS] = useState(null)
  const [msg, setMsg] = useState(''); const [error, setError] = useState(''); const [busy, setBusy] = useState(false)
  useEffect(() => { get('/api/admin/settings').then(setS) }, [])
  const save = async (e) => {
    e.preventDefault(); setMsg(''); setError('')
    try { setS(await put('/api/admin/settings', { workspace_mode: s.workspace_mode, workspace_name: s.workspace_name })); setMsg('Saved') } catch (err) { setError(err.message) }
  }
  const syncNow = async () => { setBusy(true); setError(''); setMsg(''); try { const r = await post('/api/admin/sync/catch-up'); setMsg(`Scanned ${r.scanned} new message(s): ${r.parts} part(s), ${r.events} event(s); checked ${r.checked} part(s), removed ${r.removed} file(s) no longer in the channel. Asked the other servers for their folder layout.`) } catch (err) { setError(err.message) } finally { setBusy(false) } }
  const publish = async () => { setBusy(true); setError(''); setMsg(''); try { const r = await post('/api/admin/sync/publish-layout'); setMsg(`Folder layout published in ${r.messages} message(s); other servers apply it within seconds.`) } catch (err) { setError(err.message) } finally { setBusy(false) } }
  if (!s) return null
  const shared = s.workspace_mode === 'shared'
  return (
    <Section title="Workspace">
      <form onSubmit={save} className="space-y-3">
        <label className={`block rounded-lg border p-3 cursor-pointer ${!shared ? 'border-brand-500 bg-brand-500/10' : 'border-ink-700'}`}>
          <div className="flex items-center gap-2 text-sm font-medium"><input type="radio" checked={!shared} onChange={() => setS({ ...s, workspace_mode: 'private' })} /> Private: this server owns the channel</div>
          <p className="text-xs text-ink-400 mt-1">Users on this server have private folder trees. Posts from anything else in the channel are ignored.</p>
        </label>
        <label className={`block rounded-lg border p-3 cursor-pointer ${shared ? 'border-brand-500 bg-brand-500/10' : 'border-ink-700'}`}>
          <div className="flex items-center gap-2 text-sm font-medium"><input type="radio" checked={shared} onChange={() => setS({ ...s, workspace_mode: 'shared' })} /> Shared: other servers or people use this channel too</div>
          <p className="text-xs text-ink-400 mt-1">The channel is a shared drive. Files posted by other servers are indexed here as they arrive (or on the next catch-up after downtime), their deletions, renames and moves are applied, and all users on this server see everything.</p>
        </label>
        <div><label className="label">This server's name</label><input className="input" placeholder="e.g. home" value={s.workspace_name || ''} onChange={(e) => setS({ ...s, workspace_name: e.target.value })} />
          <p className="text-xs text-ink-400 mt-1">Shown as "name/username" next to files uploaded from here. Must be different on every server that shares the channel.</p></div>
        <Alert>{error}</Alert><Alert kind="ok">{msg}</Alert>
        <div className="flex flex-wrap gap-2"><button className="btn-primary">Save</button>{shared && <button type="button" className="btn-ghost" disabled={busy} onClick={syncNow}>{busy ? 'Working…' : 'Sync with channel now'}</button>}{shared && <button type="button" className="btn-ghost" disabled={busy} onClick={publish}>Publish folder layout</button>}</div>
        {shared && <p className="text-xs text-ink-400">Sync is automatic: on start and every minute this server catches up with the channel, listens live, and exchanges folder layouts with the others. The buttons only force it now. The System panel above shows what the last sync did.</p>}
      </form>
    </Section>
  )
}
