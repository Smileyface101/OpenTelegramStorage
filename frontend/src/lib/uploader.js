// Resumable browser -> server upload. The file is sliced into chunks and each
// chunk is PUT with its offset; the server rejects out-of-order chunks with the
// offset it actually has, so a retry or reload can pick up where it stopped.
import { api, ApiError } from './api'
import { createSHA256 } from 'hash-wasm'

const MAX_TRIES = 5

const PARALLEL = 3  // chunks in flight per file

// Upload one file. Plain uploads (no bundle) use the parallel protocol: the
// server keeps a bitmap of received chunks, so chunks go up several at a
// time, retries are idempotent, and an interrupted upload resumes by sending
// only what is missing (pass resumeId). Bundle members stay sequential.
export async function uploadFile(file, { folderId = null, bundleId = null, path = null, memberIndex = null, resumeId = null, handle = null, onProgress, onStatus, onInit, signal } = {}) {
  const init = resumeId
    ? await api(`/api/uploads/${resumeId}`, { signal })
    : await api('/api/uploads', {
      method: 'POST', signal,
      body: { name: file.name, size: file.size, mime_type: file.type || null, folder_id: folderId, bundle_id: bundleId, path, member_index: memberIndex },
    })
  onInit?.(init)
  if (init.parallel) return uploadParallel(file, init, { handle, onProgress, onStatus, signal })
  return uploadSequential(file, init, { bundleId, onProgress, onStatus, signal })
}

async function uploadParallel(file, init, { handle, onProgress, onStatus, signal }) {
  const { remember, forget } = await import('./resume')
  await remember({ uploadId: init.id, fileId: init.file_id, name: file.name, size: file.size, lastModified: file.lastModified,
    folderId: init.folder_id, handle: handle || null, createdAt: Date.now() })
  const cs = init.chunk_size
  const total = init.total_chunks
  const missing = new Set(init.missing_chunks)
  const hashing = init.part_size ? await makeHasher(init.part_size, file.size) : null
  let received = init.received || 0
  let inflight = 0
  let failure = null
  const waiters = []
  const wake = () => { while (waiters.length && inflight < PARALLEL) waiters.shift()() }
  const slot = () => new Promise((r) => { if (inflight < PARALLEL) r(); else waiters.push(r) })

  const send = async (i, blob) => {
    let tries = 0
    for (;;) {
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
      try {
        const r = await api(`/api/uploads/${init.id}/chunk`, {
          method: 'PUT', body: blob, signal, headers: { 'X-Chunk-Offset': String(i * cs), 'Content-Type': 'application/octet-stream' },
        })
        received = Math.max(received, r.received); onStatus?.('uploading'); onProgress?.(received, file.size)
        return
      } catch (e) {
        if (signal?.aborted) throw e
        if (e instanceof ApiError && e.status === 429 && e.detail?.code === 'backpressure') { onStatus?.('waiting'); await sleep((e.detail.retry_after || 2) * 1000); continue }
        if (++tries >= MAX_TRIES) throw e
        await sleep(1000 * tries)
      }
    }
  }

  const tasks = []
  for (let i = 0; i < Math.max(total, 1) && i * cs < file.size; i++) {
    if (failure) break
    const blob = file.slice(i * cs, Math.min((i + 1) * cs, file.size))
    if (hashing) await hashing.feed(i * cs, blob)   // sequential read keeps the digests in order
    if (!missing.has(i)) continue
    await slot(); inflight++
    tasks.push(send(i, blob).catch((e) => { failure = failure || e }).finally(() => { inflight--; wake() }))
  }
  await Promise.all(tasks)
  if (failure) throw failure
  if (file.size === 0) onProgress?.(0, 0)
  const digests = hashing ? await hashing.finish() : undefined
  const result = await api(`/api/uploads/${init.id}/complete`, { method: 'POST', signal, body: digests })
  await forget(init.id)
  return result
}

async function uploadSequential(file, init, { bundleId, onProgress, onStatus, signal }) {
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
        onStatus?.('waiting')
        await sleep((e.detail.retry_after || 2) * 1000)
        continue
      }
      if (++tries >= MAX_TRIES) throw e
      await sleep(1000 * tries)
    }
  }
  if (file.size === 0) onProgress?.(0, 0)
  return api(`/api/uploads/${init.id}/complete`, { method: 'POST', signal })
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function makeHasher(partSize, size) {
  const whole = await createSHA256()
  let part = await createSHA256()
  const parts = []
  let fed = 0
  let partStart = 0
  const fedChunks = new Set()
  return {
    async feed(offset, blob) {
      if (offset !== fed || fedChunks.has(offset)) return  // retries re-send the same bytes; hash once
      fedChunks.add(offset)
      const buf = new Uint8Array(await blob.arrayBuffer())
      let pos = 0
      while (pos < buf.length) {
        const room = partStart + partSize - (fed + pos)
        const slice = buf.subarray(pos, pos + Math.min(room, buf.length - pos))
        whole.update(slice); part.update(slice)
        pos += slice.length
        if (fed + pos === partStart + partSize) { parts.push(part.digest('hex')); part = await createSHA256(); partStart += partSize }
      }
      fed += buf.length
    },
    async finish() {
      if (size === 0 || fed > partStart) parts.push(part.digest('hex'))
      return { sha256: whole.digest('hex'), part_sha256: parts }
    },
  }
}

// Upload several files into one archive. The member list goes first so the
// server can fix the archive layout; members are then streamed in order and
// the zip bytes flow straight into the part pipeline (never a whole file on
// disk). `items` are {file, path}; path is the name inside the archive.
export async function uploadBundle(items, { name, folderId = null, onProgress, onStatus, onInit, signal } = {}) {
  const bundle = await api('/api/bundles', {
    method: 'POST', signal,
    body: { name, folder_id: folderId, members: items.map(({ file, path }) => ({ path: path || file.name, size: file.size })) },
  })
  onInit?.({ file_id: bundle.file_id })
  const total = items.reduce((a, it) => a + it.file.size, 0)
  const SMALL = 8 * 1024 * 1024
  let doneBefore = 0
  let lastReport = 0
  try {
    for (let i = 0; i < items.length; i++) {
      const { file } = items[i]
      const member = bundle.members[i]
      if (file.size <= SMALL) {
        // One request per small file: init + bytes + descriptor in a single PUT.
        let tries = 0
        for (;;) {
          if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
          try {
            await api(`/api/bundles/${bundle.id}/members/${i}`, { method: 'PUT', body: file, signal, headers: { 'Content-Type': 'application/octet-stream' } })
            break
          } catch (e) {
            if (signal?.aborted) throw e
            if (e instanceof ApiError && e.status === 429) { onStatus?.('waiting'); await sleep((e.detail?.retry_after || 2) * 1000); continue }
            if (++tries >= MAX_TRIES) throw e
            await sleep(1000 * tries)
          }
        }
        onStatus?.('uploading')
      } else {
        await uploadFile(file, { folderId, bundleId: bundle.id, path: member.path, memberIndex: i, signal, onStatus,
          onProgress: (d) => onProgress?.(doneBefore + d, total) })
      }
      doneBefore += file.size
      const now = Date.now()
      if (now - lastReport > 200 || i === items.length - 1) { lastReport = now; onProgress?.(doneBefore, total) }
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
export function supportsFilePicker() {
  return typeof window !== 'undefined' && typeof window.showOpenFilePicker === 'function' && window.isSecureContext
}

// Pick files with the File System Access API so their handles can be
// remembered for resuming after a browser restart. Returns [{file, handle}].
export async function pickFilesWithHandles() {
  const handles = await window.showOpenFilePicker({ multiple: true })
  return Promise.all(handles.map(async (handle) => ({ file: await handle.getFile(), handle })))
}

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
