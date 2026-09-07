// Remembers unfinished uploads across page reloads and browser restarts.
// IndexedDB can hold FileSystemFileHandles (Chrome/Edge), which lets a
// remembered upload continue with one permission click; elsewhere the user
// re-picks the same file and we match it by name, size and mtime.
const DB = 'ots'; const STORE = 'uploads'

function open() {
  return new Promise((res, rej) => {
    const req = indexedDB.open(DB, 1)
    req.onupgradeneeded = () => req.result.createObjectStore(STORE, { keyPath: 'uploadId' })
    req.onsuccess = () => res(req.result); req.onerror = () => rej(req.error)
  })
}
async function tx(mode, fn) {
  try {
    const db = await open()
    return await new Promise((res, rej) => {
      const t = db.transaction(STORE, mode); const r = fn(t.objectStore(STORE))
      t.oncomplete = () => res(r?.result); t.onerror = () => rej(t.error)
    })
  } catch { return undefined }
}
export const remember = (rec) => tx('readwrite', (s) => s.put(rec))
export const forget = (uploadId) => tx('readwrite', (s) => s.delete(uploadId))
export const listRemembered = async () => (await tx('readonly', (s) => s.getAll())) || []
