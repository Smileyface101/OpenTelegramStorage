// Minimal JSON client. Cookies carry the session; the CSRF double-submit header
// is added on every state-changing call.
export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === 'string' ? detail : (detail?.message || detail?.code || `HTTP ${status}`))
    this.status = status
    this.detail = detail
  }
}

function csrf() {
  const m = document.cookie.match(/(?:^|;\s*)otg_csrf=([^;]+)/)
  return m ? decodeURIComponent(m[1]) : ''
}

export async function api(path, { method = 'GET', body, headers = {}, raw = false, signal } = {}) {
  const h = { ...headers }
  let payload = body
  if (body !== undefined && !(body instanceof Blob) && !(body instanceof ArrayBuffer)) {
    h['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }
  if (method !== 'GET') h['X-CSRF-Token'] = csrf()
  const res = await fetch(path, { method, headers: h, body: payload, credentials: 'same-origin', signal })
  if (raw) return res
  let data = null
  const text = await res.text()
  try { data = text ? JSON.parse(text) : null } catch { data = text }
  if (!res.ok) throw new ApiError(res.status, data?.detail ?? data)
  return data
}

export const get = (p, o) => api(p, o)
export const post = (p, body, o) => api(p, { ...o, method: 'POST', body })
export const put = (p, body, o) => api(p, { ...o, method: 'PUT', body })
export const patch = (p, body, o) => api(p, { ...o, method: 'PATCH', body })
export const del = (p, o) => api(p, { ...o, method: 'DELETE' })
