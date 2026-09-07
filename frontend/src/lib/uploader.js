// Resumable browser -> server upload. The file is sliced into chunks and each
// chunk is PUT with its offset; the server rejects out-of-order chunks with the
// offset it actually has, so a retry or reload can pick up where it stopped.
import { api, ApiError } from './api'

const MAX_TRIES = 5

export async function uploadFile(file, { folderId = null, bundleId = null, path = null, memberIndex = null, onProgress, onStatus, signal } = {}) {
  const init = await api('/api/uploads', {
    method: 'POST', signal,
    body: { name: file.name, size: file.size, mime_type: file.type || null, folder_id: folderId, bundle_id: bundleId, path, member_index: memberIndex },
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
      onStatus?.('uploading')
      onProgress?.(offset, file.size)
    } catch (e) {
      if (signal?.aborted) throw e
      if (e instanceof ApiError && e.status === 409 && e.detail?.received != null) { offset = e.detail.received; continue }
      if (e instanceof ApiError && e.status === 429 && e.detail?.code === 'backpressure') {
        // Staging is full: the server is still pushing earlier parts to Telegram.
        onStatus?.('waiting')
        await new Promise(r => setTimeout(r, (e.detail.retry_after || 2) * 1000))
        continue
      }
      if (++tries >= MAX_TRIES) throw e
      await new Promise(r => setTimeout(r, 1000 * tries))
    }
  }
  if (file.size === 0) onProgress?.(0, 0)
  return api(`/api/uploads/${init.id}/complete`, { method: 'POST', signal })
}

// Upload several files into one archive. The member list goes first so the
// server can fix the archive layout; members are then streamed in order and
// the zip bytes flow straight into the part pipeline (never a whole file on
// disk). `items` are {file, path}; path is the name inside the archive.
export async function uploadBundle(items, { name, folderId = null, onProgress, onStatus, signal } = {}) {
  const bundle = await api('/api/bundles', {
    method: 'POST', signal,
    body: { name, folder_id: folderId, members: items.map(({ file, path }) => ({ path: path || file.name, size: file.size })) },
  })
  const total = items.reduce((a, it) => a + it.file.size, 0)
  let doneBefore = 0
  try {
    for (let i = 0; i < items.length; i++) {
      const { file } = items[i]
      const member = bundle.members[i]
      await uploadFile(file, { folderId, bundleId: bundle.id, path: member.path, memberIndex: i, signal, onStatus,
        onProgress: (d) => onProgress?.(doneBefore + d, total) })
      doneBefore += file.size
    }
    return api(`/api/bundles/${bundle.id}/complete`, { method: 'POST', signal })
  } catch (e) {
    try { await api(`/api/bundles/${bundle.id}`, { method: 'DELETE' }) } catch { /* ignore */ }
    throw e
  }
}

// Upload a folder tree as individual files, recreating the sub-folders in the
// app under `folderId`. `items` are {file, path} with path like "root/sub/x.jpg".
export async function uploadTree(items, { folderId = null, onProgress, onStatus, onFileDone, signal } = {}) {
  const total = items.reduce((a, it) => a + it.file.size, 0)
  const folderIds = new Map()  // dir path -> folder id
  const ensure = async (dir) => {
    if (!dir) return folderId
    if (folderIds.has(dir)) return folderIds.get(dir)
    const f = await api('/api/folders/ensure', { method: 'POST', signal, body: { path: dir, parent_id: folderId } })
    folderIds.set(dir, f.id)
    return f.id
  }
  let doneBefore = 0
  for (const { file, path } of items) {
    const dir = (path || file.name).split('/').slice(0, -1).join('/')
    const target = await ensure(dir)
    await uploadFile(file, { folderId: target, signal, onStatus, onProgress: (d) => onProgress?.(doneBefore + d, total) })
    doneBefore += file.size
    onFileDone?.()
  }
}

// ---- folder picking helpers ----

// Files from <input webkitdirectory> carry webkitRelativePath ("Root/sub/a.jpg").
export function itemsFromFileList(files) {
  return Array.from(files).map((file) => ({ file, path: file.webkitRelativePath || file.name }))
}

// Drag-and-drop: walk dropped directories with the FileSystem Entry API.
// Returns {items:[{file,path}], hadDirectory}.
export async function itemsFromDataTransfer(dt) {
  const entries = Array.from(dt.items || []).map((it) => it.webkitGetAsEntry?.()).filter(Boolean)
  if (entries.length === 0) return { items: Array.from(dt.files).map((file) => ({ file, path: file.name })), hadDirectory: false }
  const items = []
  let hadDirectory = false
  const walk = async (entry, prefix) => {
    if (entry.isFile) {
      const file = await new Promise((res, rej) => entry.file(res, rej))
      items.push({ file, path: prefix + entry.name })
    } else if (entry.isDirectory) {
      hadDirectory = true
      const reader = entry.createReader()
      // readEntries returns batches; keep reading until an empty batch.
      for (;;) {
        const batch = await new Promise((res, rej) => reader.readEntries(res, rej))
        if (!batch.length) break
        for (const child of batch) await walk(child, prefix + entry.name + '/')
      }
    }
  }
  for (const e of entries) await walk(e, '')
  return { items, hadDirectory }
}

export function rootFolderName(items) {
  const first = items[0]?.path || ''
  return first.includes('/') ? first.split('/')[0] : ''
}

// File System Access API (Chrome/Edge): window.showDirectoryPicker() hands us a
// directory handle. Walking it ourselves avoids the browser having to enumerate
// the whole tree before its confirmation dialog, and lets us show progress.
export function supportsDirectoryPicker() {
  return typeof window !== 'undefined' && typeof window.showDirectoryPicker === 'function' && window.isSecureContext
}

export async function itemsFromDirectoryPicker({ onProgress, signal } = {}) {
  const root = await window.showDirectoryPicker({ mode: 'read' })
  const items = []
  const walk = async (dir, prefix) => {
    for await (const [name, handle] of dir.entries()) {
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
      if (handle.kind === 'file') {
        const file = await handle.getFile()
        items.push({ file, path: prefix + name })
        if (items.length % 50 === 0) onProgress?.(items.length)
      } else if (handle.kind === 'directory') {
        await walk(handle, prefix + name + '/')
      }
    }
  }
  await walk(root, root.name + '/')
  onProgress?.(items.length)
  return items
}
