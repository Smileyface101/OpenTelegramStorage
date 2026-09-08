import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { Download, Lock, Send, ShieldCheck } from 'lucide-react'
import { get, post } from '../lib/api'
import { bytes, when } from '../lib/format'
import { Alert } from '../components/ui'

// Public page for a share link. Needs no account.
export default function SharePage() {
  const { token } = useParams()
  const [info, setInfo] = useState(null)
  const [error, setError] = useState('')
  const [password, setPassword] = useState('')
  const [grant, setGrant] = useState(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    get(`/api/share/${token}`).then(setInfo).catch((e) => setError(e.status === 404 ? 'This link does not exist, has expired, or has reached its download limit.' : e.message))
  }, [token])
  const unlock = async (e) => {
    e.preventDefault(); setBusy(true); setError('')
    try { const r = await post(`/api/share/${token}/unlock`, { password }); setGrant(r.grant) } catch (err) { setError(err.message) } finally { setBusy(false) }
  }
  const href = `/api/share/${token}/download${grant ? `?grant=${encodeURIComponent(grant)}` : ''}`
  return (
    <div className="min-h-screen flex items-center justify-center p-4">
      <div className="card w-full max-w-md space-y-4">
        <div className="flex items-center gap-2 font-semibold">
          <span className="w-8 h-8 rounded-lg bg-brand-500 grid place-items-center text-white"><Send size={16} /></span>
          OpenTelegramStorage
        </div>
        {error && <Alert>{error}</Alert>}
        {info && (
          <>
            <div>
              <div className="text-lg font-semibold break-all">{info.name}</div>
              <div className="text-sm text-ink-400">{bytes(info.size)}{info.label ? ` · ${info.label}` : ''}</div>
              <div className="text-xs text-ink-500 mt-1 space-x-2">
                {info.expires_at && <span>expires {when(info.expires_at)}</span>}
                {info.downloads_left != null && <span>{info.downloads_left} download{info.downloads_left === 1 ? '' : 's'} left</span>}
              </div>
            </div>
            {info.requires_password && !grant ? (
              <form onSubmit={unlock} className="space-y-2">
                <label className="label flex items-center gap-1"><Lock size={12} /> This link is password protected</label>
                <input className="input" type="password" autoFocus value={password} onChange={(e) => setPassword(e.target.value)} />
                <button className="btn-primary w-full justify-center" disabled={busy || !password}>Unlock</button>
              </form>
            ) : (
              <a href={href} className="btn-primary w-full justify-center"><Download size={16} /> Download</a>
            )}
            {info.sha256 && <div className="text-[11px] text-ink-500 flex items-start gap-1"><ShieldCheck size={12} className="mt-0.5 shrink-0" /><span>SHA-256 <span className="font-mono break-all">{info.sha256}</span></span></div>}
          </>
        )}
      </div>
    </div>
  )
}
