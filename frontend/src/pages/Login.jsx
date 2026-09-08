import { useState } from 'react'
import { Send } from 'lucide-react'
import { post } from '../lib/api'
import { Alert } from '../components/ui'

export default function Login({ onAuth }) {
  const [form, setForm] = useState({ username: '', password: '' })
  const [mfa, setMfa] = useState(null)   // { token, recovery_codes_left }
  const [code, setCode] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const submit = async (e) => {
    e.preventDefault(); setBusy(true); setError('')
    try {
      const r = await post('/api/auth/login', form)
      if (r.mfa_required) { setMfa({ token: r.mfa_token, left: r.recovery_codes_left }); return }
      await onAuth()
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }
  const submitCode = async (e) => {
    e.preventDefault(); setBusy(true); setError('')
    try { await post('/api/auth/login/mfa', { mfa_token: mfa.token, code: code.trim() }); await onAuth() }
    catch (err) { setError(err.status === 401 && err.message === 'Sign in again' ? 'Session expired, sign in again.' : err.message); if (err.message === 'Sign in again') { setMfa(null); setCode('') } }
    finally { setBusy(false) }
  }
  if (mfa) return (
    <div className="min-h-screen flex items-center justify-center p-4">
      <form onSubmit={submitCode} className="card w-full max-w-sm space-y-4">
        <div className="font-semibold text-lg">Two-factor code</div>
        <p className="text-sm text-ink-300">Enter the 6-digit code from your authenticator app, or one of your recovery codes.</p>
        <input className="input text-center text-lg tracking-widest" autoFocus autoComplete="one-time-code" inputMode="numeric" placeholder="123456" value={code} onChange={(e) => setCode(e.target.value)} />
        <Alert>{error}</Alert>
        <button className="btn-primary w-full justify-center" disabled={busy || !code}>Verify</button>
        <button type="button" className="text-xs text-ink-400 hover:text-white w-full" onClick={() => { setMfa(null); setCode(''); setError('') }}>Back</button>
      </form>
    </div>
  )
  return (
    <div className="min-h-screen flex items-center justify-center p-4">
      <form onSubmit={submit} className="card w-full max-w-sm space-y-4">
        <div className="flex items-center gap-2 font-semibold text-lg">
          <span className="w-9 h-9 rounded-lg bg-brand-500 grid place-items-center text-white"><Send size={18} /></span>
          OpenTelegramStorage
        </div>
        <div><label className="label">Username</label>
          <input className="input" autoFocus autoComplete="username" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} /></div>
        <div><label className="label">Password</label>
          <input className="input" type="password" autoComplete="current-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></div>
        <Alert>{error}</Alert>
        <button className="btn-primary w-full justify-center" disabled={busy}>Sign in</button>
      </form>
    </div>
  )
}
