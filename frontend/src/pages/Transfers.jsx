import { useEffect, useState } from 'react'
import { get } from '../lib/api'
import { bytes, when, pct } from '../lib/format'
import { Progress, StatusBadge } from '../components/ui'

export default function Transfers() {
  const [data, setData] = useState(null)
  useEffect(() => {
    let alive = true
    const load = async () => { try { const d = await get('/api/transfers'); if (alive) setData(d) } catch { /* retry next tick */ } }
    load(); const t = setInterval(load, 2000)
    return () => { alive = false; clearInterval(t) }
  }, [])
  if (!data) return <div className="text-sm text-ink-400">Loading…</div>
  return (
    <div className="space-y-6">
      <section>
        <h2 className="font-semibold mb-2">In progress ({data.active.length})</h2>
        <div className="card p-0 divide-y divide-ink-800">
          {data.active.length === 0 && <div className="p-6 text-sm text-ink-400">Nothing queued. Files you upload appear here until they are fully in the channel.</div>}
          {data.active.map((f) => (
            <div key={f.id} className="p-4">
              <div className="flex items-center justify-between gap-3 text-sm">
                <span className="truncate">{f.name}</span>
                <span className="flex items-center gap-3 text-ink-400 shrink-0">
                  {f.parts_total > 0 && <span>part {Math.min(f.parts_uploaded + 1, f.parts_total)}/{f.parts_total}</span>}
                  <span>{bytes(f.bytes_done)} / {bytes(f.size)}</span>
                  <StatusBadge status={f.status} />
                </span>
              </div>
              <Progress value={pct(f.bytes_done, f.size)} className="mt-2" />
              {f.error && <div className="text-xs text-red-300 mt-1">{f.error}{f.retries ? ` (retry ${f.retries})` : ''}</div>}
            </div>
          ))}
        </div>
      </section>
      <section>
        <h2 className="font-semibold mb-2">Recently completed</h2>
        <div className="card p-0 divide-y divide-ink-800">
          {data.recent.length === 0 && <div className="p-6 text-sm text-ink-400">No completed transfers yet.</div>}
          {data.recent.map((f) => (
            <div key={f.id} className="p-3 flex items-center justify-between text-sm gap-3">
              <span className="truncate">{f.name}</span>
              <span className="text-ink-400 shrink-0">{bytes(f.size)} · {f.parts_total} part(s) · {when(f.ready_at)}</span>
            </div>
          ))}
        </div>
      </section>
    </div>
  )
}
