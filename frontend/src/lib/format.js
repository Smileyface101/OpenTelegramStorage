export function bytes(n) {
  if (n == null) return '—'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0; let v = Number(n)
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${v < 10 && i > 0 ? v.toFixed(2) : v < 100 && i > 0 ? v.toFixed(1) : Math.round(v)} ${u[i]}`
}

export function when(iso) {
  if (!iso) return ''
  const d = new Date(iso.endsWith('Z') ? iso : iso + 'Z')
  return d.toLocaleString()
}

export function pct(done, total) {
  if (!total) return 100
  return Math.min(100, Math.round((done / total) * 100))
}
