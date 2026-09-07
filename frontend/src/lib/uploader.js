// Resumable browser -> server upload. The file is sliced into chunks and each
// chunk is PUT with its offset; the server rejects out-of-order chunks with the
// offset it actually has, so a retry or reload can pick up where it stopped.
import { api, ApiError } from './api'

const MAX_TRIES = 5

export async function uploadFile(file, { folderId = null, bundleId = null, onProgress, signal } = {}) {
  const init = await api('/api/uploads', {
    method: 'POST', signal,
    body: { name: file.name, size: file.size, mime_type: file.type || null, folder_id: folderId, bundle_id: bundleId },
  })
  const chunkSize = init.chunk_size
  let offset = init.received || 0
  let tries = 0
  while (offset < file.size) {
    const blob = file.slice(offset, Math.min(offset + chunkSize, file.size))
    try {
      const r = await api(`/api/uploads/${init.id}/chunk`, {
        method: 'PUT', body: blob, signal,
        headers: { 'X-Chunk-Offset': String(offset), 'Content-Type': 'application/octet-stream' },
      })
      offset = r.received
      tries = 0
      onProgress?.(offset, file.size)
    } catch (e) {
      if (signal?.aborted) throw e
      if (e instanceof ApiError && e.status === 409 && e.detail?.received != null) { offset = e.detail.received; continue }
      if (++tries >= MAX_TRIES) throw e
      await new Promise(r => setTimeout(r, 1000 * tries))
    }
  }
  if (file.size === 0) onProgress?.(0, 0)
  return api(`/api/uploads/${init.id}/complete`, { method: 'POST', signal })
}

// Upload several files into one server-side zip.
export async function uploadBundle(files, { name, folderId = null, compress = null, onProgress, signal } = {}) {
  const bundle = await api('/api/bundles', { method: 'POST', signal, body: { name, folder_id: folderId, compress } })
  const total = files.reduce((a, f) => a + f.size, 0)
  let doneBefore = 0
  try {
    for (const f of files) {
      await uploadFile(f, { folderId, bundleId: bundle.id, signal, onProgress: (d) => onProgress?.(doneBefore + d, total) })
      doneBefore += f.size
    }
    return api(`/api/bundles/${bundle.id}/complete`, { method: 'POST', signal })
  } catch (e) {
    try { await api(`/api/bundles/${bundle.id}`, { method: 'DELETE' }) } catch { /* ignore */ }
    throw e
  }
}
