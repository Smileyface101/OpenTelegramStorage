import { useEffect } from 'react'
import { X } from 'lucide-react'

export function Modal({ title, onClose, children }) {
  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4" onMouseDown={onClose}>
      <div className="card w-full max-w-md" onMouseDown={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h2 className="font-semibold">{title}</h2>
          <button onClick={onClose} className="text-ink-400 hover:text-white"><X size={18} /></button>
        </div>
        {children}
      </div>
    </div>
  )
}

export function Alert({ kind = 'error', children }) {
  if (!children) return null
  const cls = kind === 'error' ? 'border-red-500/30 bg-red-500/10 text-red-200' : 'border-emerald-500/30 bg-emerald-500/10 text-emerald-200'
  return <div className={`rounded-lg border px-3 py-2 text-sm ${cls}`}>{children}</div>
}

export function Progress({ value, className = '' }) {
  return (
    <div className={`h-1.5 w-full rounded bg-ink-700 overflow-hidden ${className}`}>
      <div className="h-full bg-brand-500 transition-[width]" style={{ width: `${value}%` }} />
    </div>
  )
}

export function StatusBadge({ status }) {
  const map = {
    ready: 'bg-emerald-500/15 text-emerald-300', queued: 'bg-ink-700 text-ink-300', receiving: 'bg-amber-500/15 text-amber-300', syncing: 'bg-violet-500/15 text-violet-300',
    hashing: 'bg-sky-500/15 text-sky-300', uploading: 'bg-brand-500/15 text-brand-400', failed: 'bg-red-500/15 text-red-300',
  }
  return <span className={`rounded px-2 py-0.5 text-xs font-medium ${map[status] || map.queued}`}>{status}</span>
}
