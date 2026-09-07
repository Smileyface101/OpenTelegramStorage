import { useState } from 'react'
import { Send } from 'lucide-react'
import { post } from '../lib/api'
import { Alert } from '../components/ui'

export default function Login({ onAuth }) {
  const [form, setForm] = useState({ username: '', password: '' })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const submit = async (e) => {
    e.preventDefault(); setBusy(true); setError('')
    try { await post('/api/auth/login', form); await onAuth() } catch (err) { setError(err.message) } finally { setBusy(false) }
  }
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
