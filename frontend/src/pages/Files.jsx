import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams, Link, useNavigate } from 'react-router-dom'
import {
  Folder, FolderOpen, FolderPlus, FolderUp, Upload, Download, Trash2, Pencil, Archive, RefreshCw, ChevronRight, ChevronDown,
  Search, FolderInput, ArrowUp, ArrowDown, X, Home, CornerLeftUp, Image, Film, Music, FileText, FileArchive, File as FileIcon, HardDrive, ShieldCheck, ShieldAlert, ShieldQuestion, Link2, Copy, Trash, Lock,
} from 'lucide-react'
import { get, post, del, patch } from '../lib/api'
import { uploadFile, uploadBundle, uploadTree, itemsFromFileList, itemsFromDataTransfer, itemsFromDirectoryPicker, supportsDirectoryPicker, supportsFilePicker, pickFilesWithHandles, rootFolderName } from '../lib/uploader'
import { listRemembered, forget as forgetUpload } from '../lib/resume'
import { bytes, when, pct } from '../lib/format'
import { Modal, Alert, Progress, StatusBadge } from '../components/ui'

const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`)
const DRAG_MIME = 'application/x-ots-move'

const ICONS = [
  [/\.(jpe?g|png|gif|webp|heic|bmp|svg|avif)$/i, Image, 'text-pink-300'],
  [/\.(mp4|mkv|mov|avi|webm|m4v|ts)$/i, Film, 'text-violet-300'],
  [/\.(mp3|flac|wav|m4a|ogg|aac)$/i, Music, 'text-emerald-300'],
  [/\.(zip|rar|7z|tar|gz|bz2|xz|iso)$/i, FileArchive, 'text-amber-300'],
  [/\.(pdf|docx?|xlsx?|pptx?|txt|md|csv|rtf|odt)$/i, FileText, 'text-sky-300'],
]
function fileIcon(name) {
  for (const [re, Icon, cls] of ICONS) if (re.test(name)) return <Icon size={18} className={`${cls} shrink-0`} />
  return <FileIcon size={18} className="text-ink-400 shrink-0" />
}

export default function Files({ user }) {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const folderId = params.get('folder') ? Number(params.get('folder')) : null
  const goTo = (id) => navigate(id == null ? '/files' : `/files?folder=${id}`)

  const [data, setData] = useState(null)
  const [tree, setTree] = useState([])
  const [stats, setStats] = useState(null)
  const [q, setQ] = useState('')
  const [error, setError] = useState('')
  const [uploads, setUploads] = useState([])
  const [modal, setModal] = useState(null)
  const [uploadMenu, setUploadMenu] = useState(false)
  const fileInput = useRef(null)
  const zipInput = useRef(null)
  const dirInput = useRef(null)
  const [dragging, setDragging] = useState(false)
  const [selected, setSelected] = useState({ files: new Set(), folders: new Set() })
  const [sort, setSort] = useState(() => { try { return JSON.parse(localStorage.getItem('ots.sort')) || { key: 'created_at', dir: 'desc' } } catch { return { key: 'created_at', dir: 'desc' } } })
  const [dropTarget, setDropTarget] = useState(null)
  const internalDrag = useRef(null)
  const [scanning, setScanning] = useState(null)  // number of files found while reading a picked folder
  const [pendingUploads, setPendingUploads] = useState([])  // unfinished uploads known to the server
  const resumeInput = useRef(null)
  const resumeTarget = useRef(null)

  // ---------------------------------------------------------------- data
  const load = useCallback(async () => {
    try {
      const qs = new URLSearchParams()
      if (folderId != null) qs.set('folder_id', folderId)
      if (q) qs.set('q', q)
      const [d, t, s] = await Promise.all([get(`/api/files?${qs}`), get('/api/folders/tree'), get('/api/files/stats')])
      setData(d); setTree(t); setStats(s)
    } catch (e) { setError(e.message) }
  }, [folderId, q])
  useEffect(() => { load(); setSelected({ files: new Set(), folders: new Set() }) }, [load])
  // Live refresh: the server bumps a revision counter on every index change,
  // including changes synced from other servers; reload the listing when it moves.
  const revRef = useRef(null)
  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const { revision } = await get('/api/files/revision')
        if (!alive) return
        if (revRef.current !== null && revision !== revRef.current) load()
        revRef.current = revision
      } catch { /* offline; try again next tick */ }
    }
    tick(); const t = setInterval(tick, 3000)
    return () => { alive = false; clearInterval(t) }
  }, [load])
  const pending = data?.files.some((f) => f.status !== 'ready' && f.status !== 'failed')  // includes 'syncing' from other servers
  useEffect(() => { if (!pending) return; const t = setInterval(load, 2000); return () => clearInterval(t) }, [pending, load])
  useEffect(() => { try { localStorage.setItem('ots.sort', JSON.stringify(sort)) } catch { /* ignore */ } }, [sort])
  useEffect(() => { if (!uploadMenu) return; const close = () => setUploadMenu(false); window.addEventListener('click', close); return () => window.removeEventListener('click', close) }, [uploadMenu])

  const parentId = data?.breadcrumbs?.length ? (data.breadcrumbs.length > 1 ? data.breadcrumbs[data.breadcrumbs.length - 2].id : null) : null

  // ---------------------------------------------------------------- sorting
  const toggleSort = (key) => setSort((s) => s.key === key ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: key === 'name' ? 'asc' : 'desc' })
  const cmp = (a, b) => {
    const dir = sort.dir === 'asc' ? 1 : -1
    if (sort.key === 'name') return a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' }) * dir
    if (sort.key === 'size') return ((a.size ?? -1) - (b.size ?? -1)) * dir
    if (sort.key === 'status') return String(a.status ?? '').localeCompare(String(b.status ?? '')) * dir
    return (new Date(a.created_at) - new Date(b.created_at)) * dir
  }
  const sortedFolders = data ? [...data.folders].sort(cmp) : []
  const sortedFiles = data ? [...data.files].sort(cmp) : []

  // ---------------------------------------------------------------- selection + move
  const toggleSel = (kind, id) => setSelected((s) => { const n = new Set(s[kind]); n.has(id) ? n.delete(id) : n.add(id); return { ...s, [kind]: n } })
  const selCount = selected.files.size + selected.folders.size
  const allIds = { files: (data?.files || []).map((f) => f.id), folders: (data?.folders || []).map((d) => d.id) }
  const allSelected = data && selCount > 0 && selCount === allIds.files.length + allIds.folders.length
  const toggleAll = () => setSelected(allSelected ? { files: new Set(), folders: new Set() } : { files: new Set(allIds.files), folders: new Set(allIds.folders) })
  const clearSel = () => setSelected({ files: new Set(), folders: new Set() })

  const moveItems = async ({ files, folders }, targetFolderId) => {
    if (!files.length && !folders.length) return
    if (folders.includes(targetFolderId)) return
    try {
      await post('/api/move', { file_ids: files, folder_ids: folders, target_folder_id: targetFolderId })
      clearSel(); setModal(null); await load()
    } catch (e) { setError(e.message) }
  }

  // ---------------------------------------------------------------- uploads
  const track = (name, size) => {
    const id = uuid(); const ctrl = new AbortController()
    setUploads((u) => [...u, { id, name, size, done: 0, ctrl, status: 'uploading', fileId: null }])
    return {
      ctrl,
      onStatus: (st) => setUploads((u) => u.map((x) => x.id === id ? { ...x, status: st } : x)),
      onProgress: (d) => setUploads((u) => u.map((x) => x.id === id ? { ...x, done: d } : x)),
      onInit: (init) => { setUploads((u) => u.map((x) => x.id === id ? { ...x, fileId: init.file_id } : x)); load() },
      finish: () => setUploads((u) => u.filter((x) => x.id !== id)),
    }
  }
  // Browser-side upload state for a file row (so the row shows both hops).
  const uploadFor = (fileId) => uploads.find((u) => u.fileId === fileId)
  const visibleFileIds = new Set((data?.files || []).map((f) => f.id))
  const cardUploads = uploads.filter((u) => !u.fileId || !visibleFileIds.has(u.fileId))
  const startUploads = (files, asZip = false, target = folderId) => {
    if (!files.length) return
    if (asZip) return setModal({ type: 'zip', items: files.map((file) => ({ file, path: file.name })), target })
    files.forEach((f) => runUpload(f, target))
  }
  const startFolder = (items, target = folderId) => {
    if (!items.length) { setError('The selected folder contains no files (or the browser did not grant access to it). Try dragging the folder onto the page instead.'); return }
    setModal({ type: 'folder-upload', items, root: rootFolderName(items) || 'folder', target })
  }
  const runUpload = async (file, target = folderId, { handle = null, resumeId = null } = {}) => {
    const t = track(file.name, file.size)
    try {
      await uploadFile(file, { folderId: target, handle, resumeId, signal: t.ctrl.signal, onProgress: t.onProgress, onStatus: t.onStatus, onInit: t.onInit })
      await load(); await loadPending()
    } catch (e) { if (!t.ctrl.signal.aborted) setError(`${file.name}: ${e.message}`) }
    finally { t.finish() }
  }

  // ---- interrupted uploads (page reload / browser restart) ----
  const loadPending = useCallback(async () => {
    try {
      const [server, local] = await Promise.all([get('/api/uploads'), listRemembered()])
      const byId = Object.fromEntries(local.map((r) => [r.uploadId, r]))
      const active = new Set(uploads.map((u) => u.uploadId).filter(Boolean))
      setPendingUploads(server.filter((u) => !active.has(u.id)).map((u) => ({ ...u, local: byId[u.id] || null })))
      for (const r of local) if (!server.some((u) => u.id === r.uploadId)) forgetUpload(r.uploadId)
    } catch { /* ignore */ }
  }, [uploads])
  useEffect(() => { loadPending() }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  const resumeUpload = async (p) => {
    setError('')
    const h = p.local?.handle
    if (h) {
      try {
        const perm = await h.requestPermission({ mode: 'read' })
        if (perm !== 'granted') throw new Error('permission denied')
        const file = await h.getFile()
        if (file.size !== p.size) throw new Error('the file on disk has changed size')
        setPendingUploads((l) => l.filter((x) => x.id !== p.id))
        return runUpload(file, p.folder_id, { handle: h, resumeId: p.id })
      } catch (e) { setError(`Could not reopen ${p.name} (${e.message}); please pick the file again.`) }
    }
    resumeTarget.current = p
    resumeInput.current.click()
  }
  const onPickResume = async (e) => {
    const p = resumeTarget.current; const file = e.target.files[0]; e.target.value = ''
    if (!p || !file) return
    if (file.name !== p.name || file.size !== p.size) { setError(`That is not the same file: expected ${p.name} (${bytes(p.size)}).`); return }
    setPendingUploads((l) => l.filter((x) => x.id !== p.id))
    runUpload(file, p.folder_id, { resumeId: p.id })
  }
  const discardUpload = async (p) => {
    try { await del(`/api/uploads/${p.id}`); await forgetUpload(p.id); setPendingUploads((l) => l.filter((x) => x.id !== p.id)); await load() } catch (e) { setError(e.message) }
  }

  const pickFiles = async () => {
    if (supportsFilePicker()) {
      try {
        const picked = await pickFilesWithHandles()
        picked.forEach(({ file, handle }) => runUpload(file, folderId, { handle }))
      } catch (err) { if (err?.name !== 'AbortError') setError(err.message) }
      return
    }
    fileInput.current.click()
  }
  const runZip = async (items, name, target = folderId) => {
    setModal(null)
    const t = track(`${name}.zip`, items.reduce((a, it) => a + it.file.size, 0))
    try { await uploadBundle(items, { name, folderId: target, signal: t.ctrl.signal, onProgress: t.onProgress, onStatus: t.onStatus, onInit: t.onInit }); await load() }
    catch (e) { if (!t.ctrl.signal.aborted) setError(`${name}.zip: ${e.message}`) }
    finally { t.finish() }
  }
  const runTree = async (items, root, target = folderId) => {
    setModal(null)
    const t = track(`${root}/ (${items.length} files)`, items.reduce((a, it) => a + it.file.size, 0))
    try { await uploadTree(items, { folderId: target, signal: t.ctrl.signal, onProgress: t.onProgress, onStatus: t.onStatus, onFileDone: load }); await load() }
    catch (e) { if (!t.ctrl.signal.aborted) setError(`${root}: ${e.message}`) }
    finally { t.finish() }
  }
  const onPickFolder = (e) => {
    try { startFolder(itemsFromFileList(e.target.files)) }
    catch (err) { setError(`Could not read the folder: ${err.message}`) }
    finally { e.target.value = '' }
  }
  const openFolderPicker = async () => {
    // Prefer the File System Access API: Chrome then asks a short "view files?"
    // question instead of enumerating the whole tree first, and we can walk
    // the folder ourselves with a progress counter.
    if (supportsDirectoryPicker()) {
      setScanning(0)
      try {
        const items = await itemsFromDirectoryPicker({ onProgress: setScanning })
        startFolder(items)
      } catch (err) {
        if (err?.name !== 'AbortError') setError(`Could not read the folder: ${err.message}`)
      } finally { setScanning(null) }
      return
    }
    const el = dirInput.current
    if (!el || !('webkitdirectory' in el)) { setError('This browser cannot pick folders. Drag the folder onto the page instead.'); return }
    el.click()
  }

  // ---------------------------------------------------------------- drag & drop
  const isInternal = (e) => Array.from(e.dataTransfer.types || []).includes(DRAG_MIME)
  const isExternal = (e) => Array.from(e.dataTransfer.types || []).includes('Files')
  const onRowDragStart = (e, kind, id) => {
    const inSel = selected[kind].has(id)
    internalDrag.current = inSel ? { files: [...selected.files], folders: [...selected.folders] }
      : { files: kind === 'files' ? [id] : [], folders: kind === 'folders' ? [id] : [] }
    e.dataTransfer.setData(DRAG_MIME, '1'); e.dataTransfer.effectAllowed = 'move'
  }
  const onRowDragEnd = () => { internalDrag.current = null; setDropTarget(null) }
  const onTargetDragOver = (e, target) => {
    if (!isInternal(e) && !isExternal(e)) return
    e.preventDefault(); e.stopPropagation()
    e.dataTransfer.dropEffect = isInternal(e) ? 'move' : 'copy'
    setDropTarget(target); setDragging(false)
  }
  const onTargetDrop = async (e, targetFolderId) => {
    if (isInternal(e)) {
      e.preventDefault(); e.stopPropagation(); setDropTarget(null)
      const payload = internalDrag.current; internalDrag.current = null
      if (payload) moveItems(payload, targetFolderId)
      return
    }
    if (!isExternal(e)) return
    e.preventDefault(); e.stopPropagation(); setDropTarget(null); setDragging(false)
    try {
      const { items, hadDirectory } = await itemsFromDataTransfer(e.dataTransfer)
      if (hadDirectory) startFolder(items, targetFolderId); else startUploads(items.map((it) => it.file), false, targetFolderId)
    } catch (err) { setError(err.message) }
  }
  const onPageDrop = async (e) => {
    e.preventDefault(); setDragging(false)
    if (isInternal(e)) return
    try {
      const { items, hadDirectory } = await itemsFromDataTransfer(e.dataTransfer)
      if (hadDirectory) startFolder(items); else startUploads(items.map((it) => it.file))
    } catch (err) { setError(err.message) }
  }
  const dropProps = (id) => ({
    onDragOver: (e) => onTargetDragOver(e, id ?? 'root'),
    onDragLeave: () => setDropTarget(null),
    onDrop: (e) => onTargetDrop(e, id),
  })
  const isTarget = (id) => dropTarget === (id ?? 'root')

  // ---------------------------------------------------------------- actions
  const removeFile = async (f) => {
    if (!confirm(`Delete "${f.name}" from the channel? This cannot be undone.`)) return
    try { await del(`/api/files/${f.id}`); await load() } catch (e) { setError(e.message) }
  }
  const removeFolder = async (d) => {
    if (!confirm(`Delete folder "${d.name}" and everything in it?`)) return
    try { await del(`/api/folders/${d.id}`); await load() } catch (e) { setError(e.message) }
  }
  const retry = async (f) => { try { await post(`/api/files/${f.id}/retry`); await load() } catch (e) { setError(e.message) } }
  const verify = async (f) => { try { await post(`/api/files/${f.id}/verify`); await load() } catch (e) { setError(e.message) } }
  const verifying = data?.files.some((f) => f.verifying)
  useEffect(() => { if (!verifying) return; const t = setInterval(load, 1500); return () => clearInterval(t) }, [verifying, load])

  const crumbs = data?.breadcrumbs || []
  const currentName = crumbs.length ? crumbs[crumbs.length - 1].name : 'All files'

  return (
    <div className="space-y-4" onDragOver={(e) => { e.preventDefault(); if (isExternal(e) && !isInternal(e)) setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={onPageDrop}>
      {/* ---- Path bar ---- */}
      <div className="flex flex-wrap items-center gap-2">
        <button className="btn-ghost" disabled={folderId == null} onClick={() => goTo(parentId)} title="Up one level"><CornerLeftUp size={16} /><span className="hidden sm:inline">Up</span></button>
        <nav aria-label="Folder path" className="flex-1 min-w-0 flex items-center gap-1 rounded-xl bg-ink-900 border border-ink-800 px-2 py-1.5 overflow-x-auto">
          <Crumb active={folderId == null} target={isTarget(null)} onClick={() => goTo(null)} {...dropProps(null)}><Home size={15} /><span>All files</span></Crumb>
          {crumbs.map((c, i) => (
            <span key={c.id} className="flex items-center gap-1 shrink-0">
              <ChevronRight size={14} className="text-ink-500" />
              <Crumb active={i === crumbs.length - 1} target={isTarget(c.id)} onClick={() => goTo(c.id)} {...dropProps(c.id)}><Folder size={15} className="text-amber-300" /><span>{c.name}</span></Crumb>
            </span>
          ))}
        </nav>
        <div className="relative">
          <Search size={14} className="absolute left-2.5 top-2.5 text-ink-400" />
          <input className="input pl-8 w-44 md:w-56" placeholder="Search all files" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-[220px_1fr]">
        {/* ---- Folder tree ---- */}
        <aside className="hidden lg:block">
          <FolderTree tree={tree} currentId={folderId} onNavigate={goTo} dropProps={dropProps} isTarget={isTarget} />
          {stats && (
            <div className="mt-4 rounded-xl bg-ink-900 border border-ink-800 p-3 text-xs text-ink-400 space-y-1">
              <div className="flex items-center gap-2 text-ink-300"><HardDrive size={14} /> Stored in channel</div>
              <div className="text-lg font-semibold text-ink-200">{bytes(stats.bytes)}</div>
              <div>{stats.files} file{stats.files === 1 ? '' : 's'}{stats.pending ? ` · ${stats.pending} transferring` : ''}</div>
            </div>
          )}
        </aside>

        {/* ---- Main ---- */}
        <div className="min-w-0 space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="font-semibold text-lg mr-auto truncate flex items-center gap-2">
              {folderId == null ? <Home size={18} className="text-ink-400" /> : <FolderOpen size={18} className="text-amber-300" />}{currentName}
            </h1>
            <button className="btn-ghost" onClick={() => setModal({ type: 'folder' })}><FolderPlus size={16} /> New folder</button>
            <div className="relative">
              <button className="btn-primary" onClick={(e) => { e.stopPropagation(); setUploadMenu((v) => !v) }}><Upload size={16} /> Upload <ChevronDown size={14} /></button>
              {uploadMenu && (
                <div className="absolute right-0 mt-1 w-64 rounded-xl bg-ink-800 border border-ink-700 shadow-xl z-20 p-1 text-sm" onClick={(e) => e.stopPropagation()}>
                  <MenuItem icon={Upload} title="Files" hint="One entry per file" onClick={() => { setUploadMenu(false); pickFiles() }} />
                  <MenuItem icon={FolderUp} title="Folder" hint="Zip it, or keep the tree" onClick={() => { setUploadMenu(false); openFolderPicker() }} />
                  <MenuItem icon={Archive} title="Files as one zip" hint="Pick several files, get one archive" onClick={() => { setUploadMenu(false); zipInput.current.click() }} />
                  {user?.role === 'admin' && <MenuItem icon={HardDrive} title="Import from server" hint="Files already on this machine, read in place" onClick={() => { setUploadMenu(false); setModal({ type: 'import' }) }} />}
                  <div className="px-3 py-2 text-xs text-ink-400 border-t border-ink-700 mt-1">…or drop files and folders anywhere on the page, or onto a folder to upload into it.</div>
                </div>
              )}
            </div>
            <input ref={fileInput} type="file" multiple hidden onChange={(e) => { startUploads(Array.from(e.target.files)); e.target.value = '' }} />
            <input ref={zipInput} type="file" multiple hidden onChange={(e) => { startUploads(Array.from(e.target.files), true); e.target.value = '' }} />
            <input ref={dirInput} type="file" webkitdirectory="" directory="" multiple hidden onChange={onPickFolder} />
            <input ref={resumeInput} type="file" hidden onChange={onPickResume} />
          </div>

          {pendingUploads.length > 0 && (
            <div className="rounded-xl bg-amber-500/10 border border-amber-500/30 px-3 py-2 text-sm space-y-1">
              <div className="font-medium text-amber-200">{pendingUploads.length} interrupted upload{pendingUploads.length === 1 ? '' : 's'}</div>
              {pendingUploads.map((p) => (
                <div key={p.id} className="flex items-center gap-2">
                  <span className="truncate flex-1">{p.name} <span className="text-ink-400 text-xs">{bytes(p.received)} of {bytes(p.size)} received</span></span>
                  <button className="btn-ghost" onClick={() => resumeUpload(p)}>{p.local?.handle ? 'Resume' : 'Resume (pick file)'}</button>
                  <button className="btn-danger" onClick={() => discardUpload(p)}>Discard</button>
                </div>
              ))}
            </div>
          )}
          {selCount > 0 && (
            <div className="flex items-center gap-2 rounded-xl bg-brand-500/10 border border-brand-500/30 px-3 py-2 text-sm">
              <span className="font-medium">{selCount} selected</span>
              <button className="btn-ghost" onClick={() => setModal({ type: 'move', items: { files: [...selected.files], folders: [...selected.folders] } })}><FolderInput size={16} /> Move to…</button>
              <span className="text-xs text-ink-400 hidden sm:inline">or drag the rows onto a folder</span>
              <button className="ml-auto text-ink-400 hover:text-white" onClick={clearSel}><X size={16} /></button>
            </div>
          )}

          <Alert>{error && <span className="flex justify-between gap-3">{error}<button onClick={() => setError('')}>✕</button></span>}</Alert>
          {scanning !== null && (
            <div className="flex items-center gap-3 rounded-xl bg-ink-900 border border-ink-800 px-3 py-2 text-sm">
              <span className="w-4 h-4 border-2 border-brand-400 border-t-transparent rounded-full animate-spin" />
              Reading folder… {scanning} file{scanning === 1 ? '' : 's'} found
            </div>
          )}

          {cardUploads.length > 0 && (
            <div className="card space-y-3">
              <div className="text-xs uppercase tracking-wide text-ink-400">Starting uploads <span className="normal-case text-ink-500">· each file gets its own row below as soon as it is registered</span></div>
              {cardUploads.map((u) => (
                <div key={u.id}>
                  <div className="flex justify-between text-sm"><span className="truncate">{u.name}{u.status === 'waiting' && <span className="ml-2 text-xs text-amber-300">waiting for Telegram to catch up…</span>}</span>
                    <span className="text-ink-400 flex items-center gap-2 shrink-0">{bytes(u.done)} / {bytes(u.size)}
                      <button onClick={() => u.ctrl.abort()} className="text-red-300 hover:text-red-200">cancel</button></span></div>
                  <Progress value={pct(u.done, u.size)} className="mt-1" />
                </div>
              ))}
            </div>
          )}

          <div className={`card p-0 overflow-hidden transition-shadow ${dragging ? 'ring-2 ring-brand-500 shadow-lg shadow-brand-500/20' : ''}`}>
            {!data ? <div className="p-6 text-sm text-ink-400">Loading…</div> : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="text-xs uppercase text-ink-400 bg-ink-800/50 select-none">
                    <tr>
                      <th className="p-3 w-8"><input type="checkbox" checked={!!allSelected} onChange={toggleAll} disabled={(allIds.files.length + allIds.folders.length) === 0} /></th>
                      <SortTh label="Name" k="name" sort={sort} onClick={toggleSort} className="text-left" />
                      <SortTh label="Size" k="size" sort={sort} onClick={toggleSort} className="text-right hidden sm:table-cell" />
                      <SortTh label="Status" k="status" sort={sort} onClick={toggleSort} className="text-left hidden md:table-cell" />
                      <SortTh label="Added" k="created_at" sort={sort} onClick={toggleSort} className="text-left hidden lg:table-cell" />
                      <th className="p-3" />
                    </tr>
                  </thead>
                  <tbody>
                    {sortedFolders.map((d) => (
                      <tr key={`d${d.id}`} draggable onDragStart={(e) => onRowDragStart(e, 'folders', d.id)} onDragEnd={onRowDragEnd} {...dropProps(d.id)}
                        className={`group border-t border-ink-800 hover:bg-ink-800/40 ${selected.folders.has(d.id) ? 'bg-brand-500/10' : ''} ${isTarget(d.id) ? 'bg-brand-500/20 ring-1 ring-inset ring-brand-500' : ''}`}>
                        <td className="p-3"><input type="checkbox" checked={selected.folders.has(d.id)} onChange={() => toggleSel('folders', d.id)} /></td>
                        <td className="p-3">
                          <button onClick={() => goTo(d.id)} className="flex items-center gap-2 font-medium hover:underline text-left">
                            <Folder size={18} className="text-amber-300 shrink-0" />{d.name}
                            {isTarget(d.id) && <span className="text-xs text-brand-400 font-normal">drop here</span>}
                          </button>
                        </td>
                        <td className="hidden sm:table-cell" /><td className="hidden md:table-cell" /><td className="p-3 text-ink-400 hidden lg:table-cell">{when(d.created_at)}</td>
                        <td className="p-3"><RowActions>
                          <IconBtn title="Move" onClick={() => setModal({ type: 'move', items: { files: [], folders: [d.id] } })}><FolderInput size={16} /></IconBtn>
                          <IconBtn title="Delete" danger onClick={() => removeFolder(d)}><Trash2 size={16} /></IconBtn>
                        </RowActions></td>
                      </tr>
                    ))}
                    {sortedFiles.map((f) => (
                      <tr key={f.id} draggable onDragStart={(e) => onRowDragStart(e, 'files', f.id)} onDragEnd={onRowDragEnd}
                        className={`group border-t border-ink-800 hover:bg-ink-800/40 ${selected.files.has(f.id) ? 'bg-brand-500/10' : ''}`}>
                        <td className="p-3"><input type="checkbox" checked={selected.files.has(f.id)} onChange={() => toggleSel('files', f.id)} /></td>
                        <td className="p-3">
                          <div className="flex items-center gap-2 min-w-0">{f.is_archive ? <FileArchive size={18} className="text-amber-300 shrink-0" /> : fileIcon(f.name)}
                            <span className="truncate">{f.name}</span>
                            {f.parts_total > 1 && <span className="text-xs text-ink-400 shrink-0">{f.parts_total} parts</span>}
                            {f.encrypted && <Lock size={12} className="text-ink-500 shrink-0" title="Encrypted before it reached Telegram" />}
                            {f.uploaded_by && <span className="text-[11px] text-ink-500 shrink-0" title="Uploaded by">{f.uploaded_by}</span>}
                            <Integrity f={f} /></div>
                          {f.integrity_error && <div className="text-xs text-red-300 mt-1 truncate">Integrity: {f.integrity_error}</div>}
                          {f.status !== 'ready' && f.status !== 'failed' && (() => {
                            const up = uploadFor(f.id)
                            const received = up ? Math.max(up.done, f.bytes_received || 0) : (f.bytes_received || 0)
                            return (
                              <div className="mt-1 max-w-sm">
                                <div className="relative h-1.5 w-full rounded bg-ink-700 overflow-hidden" title="light: received from your browser · solid: stored in Telegram">
                                  <div className="absolute inset-y-0 left-0 bg-brand-500/30" style={{ width: `${pct(received, f.size)}%` }} />
                                  <div className="absolute inset-y-0 left-0 bg-brand-500" style={{ width: `${pct(f.bytes_done, f.size)}%` }} />
                                </div>
                                <div className="text-[11px] text-ink-400 mt-0.5 flex items-center gap-2">
                                  <span>{f.status === 'receiving' || up ? `${bytes(received)} from browser · ` : ''}{bytes(f.bytes_done)} in Telegram{f.parts_total > 1 ? ` · part ${Math.min(f.parts_uploaded + 1, f.parts_total)}/${f.parts_total}` : ''}</span>
                                  {up?.status === 'waiting' && <span className="text-amber-300">browser paused while Telegram catches up</span>}
                                  {up && <button onClick={() => up.ctrl.abort()} className="text-red-300 hover:text-red-200">cancel upload</button>}
                                </div>
                              </div>
                            )
                          })()}
                          {f.error && <div className="text-xs text-red-300 mt-1 truncate">{f.error}</div>}
                        </td>
                        <td className="p-3 text-right text-ink-300 hidden sm:table-cell whitespace-nowrap">{bytes(f.size)}</td>
                        <td className="p-3 hidden md:table-cell"><StatusBadge status={f.status} /></td>
                        <td className="p-3 text-ink-400 hidden lg:table-cell whitespace-nowrap">{when(f.created_at)}</td>
                        <td className="p-3"><RowActions>
                          {f.status === 'ready' && <a href={`/api/files/${f.id}/download`} className="p-1 rounded hover:bg-ink-700 hover:text-white" title="Download"><Download size={16} /></a>}
                          {f.status === 'failed' && <IconBtn title="Retry" onClick={() => retry(f)}><RefreshCw size={16} /></IconBtn>}
                          {f.status === 'ready' && <IconBtn title="Share link" onClick={() => setModal({ type: 'share', file: f })}><Link2 size={16} /></IconBtn>}
                          {f.status === 'ready' && <IconBtn title="Verify against the channel" onClick={() => verify(f)}><ShieldCheck size={16} /></IconBtn>}
                          <IconBtn title="Move" onClick={() => setModal({ type: 'move', items: { files: [f.id], folders: [] } })}><FolderInput size={16} /></IconBtn>
                          <IconBtn title="Rename" onClick={() => setModal({ type: 'rename', file: f })}><Pencil size={16} /></IconBtn>
                          <IconBtn title="Delete" danger onClick={() => removeFile(f)}><Trash2 size={16} /></IconBtn>
                        </RowActions></td>
                      </tr>
                    ))}
                    {data.folders.length === 0 && data.files.length === 0 && (
                      <tr><td colSpan={6} className="p-12 text-center text-ink-400">
                        <div className="mx-auto w-14 h-14 rounded-2xl bg-ink-800 grid place-items-center mb-3"><Upload size={22} className="text-ink-300" /></div>
                        <div className="text-ink-300 font-medium mb-1">{q ? 'No matches' : 'This folder is empty'}</div>
                        <div className="text-xs">Drop files or folders here, or onto a folder to upload into it. Large files are split into parts automatically.</div>
                      </td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      </div>

      {modal?.type === 'folder' && <NameModal title="New folder" onClose={() => setModal(null)} onSubmit={async (name) => { await post('/api/folders', { name, parent_id: folderId }); setModal(null); load() }} />}
      {modal?.type === 'rename' && <NameModal title="Rename" initial={modal.file.name} onClose={() => setModal(null)} onSubmit={async (name) => { await patch(`/api/files/${modal.file.id}`, { name }); setModal(null); load() }} />}
      {modal?.type === 'zip' && <ZipModal items={modal.items} onClose={() => setModal(null)} onSubmit={(items, name) => runZip(items, name, modal.target)} />}
      {modal?.type === 'move' && <MoveModal tree={tree} items={modal.items} currentFolderId={folderId} onClose={() => setModal(null)} onMove={(target) => moveItems(modal.items, target)} />}
      {modal?.type === 'share' && <ShareModal file={modal.file} onClose={() => setModal(null)} />}
      {modal?.type === 'import' && <ImportModal folderId={folderId} onClose={() => setModal(null)} onDone={() => { setModal(null); load() }} />}
      {modal?.type === 'folder-upload' && <FolderUploadModal items={modal.items} root={modal.root} onClose={() => setModal(null)} onZip={(items, name) => runZip(items, name, modal.target)} onTree={(items, root) => runTree(items, root, modal.target)} />}
    </div>
  )
}

// ------------------------------------------------------------------ pieces
function Crumb({ active, target, onClick, children, ...drop }) {
  return (
    <button onClick={onClick} {...drop}
      className={`flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-sm whitespace-nowrap transition-colors ${active ? 'bg-ink-700 text-white font-medium' : 'text-ink-300 hover:bg-ink-800 hover:text-white'} ${target ? 'ring-2 ring-brand-500 bg-brand-500/20' : ''}`}>
      {children}
    </button>
  )
}

function MenuItem({ icon: Icon, title, hint, onClick }) {
  return (
    <button onClick={onClick} className="w-full flex items-center gap-3 rounded-lg px-3 py-2 text-left hover:bg-ink-700">
      <Icon size={16} className="text-ink-300" /><span><div className="font-medium">{title}</div><div className="text-xs text-ink-400">{hint}</div></span>
    </button>
  )
}

function RowActions({ children }) {
  return <div className="flex justify-end gap-1 text-ink-400 opacity-70 group-hover:opacity-100">{children}</div>
}
function IconBtn({ title, danger, onClick, children }) {
  return <button onClick={onClick} title={title} className={`p-1 rounded hover:bg-ink-700 ${danger ? 'hover:text-red-300' : 'hover:text-white'}`}>{children}</button>
}

function FolderTree({ tree, currentId, onNavigate, dropProps, isTarget }) {
  const [collapsed, setCollapsed] = useState(() => { try { return new Set(JSON.parse(localStorage.getItem('ots.tree.collapsed')) || []) } catch { return new Set() } })
  useEffect(() => { try { localStorage.setItem('ots.tree.collapsed', JSON.stringify([...collapsed])) } catch { /* ignore */ } }, [collapsed])
  const nodes = useMemo(() => {
    const byParent = {}
    for (const f of tree) (byParent[f.parent_id ?? 'root'] ||= []).push(f)
    return byParent
  }, [tree])
  // Ancestors of the current folder are always shown open.
  const ancestors = useMemo(() => {
    const byId = Object.fromEntries(tree.map((f) => [f.id, f])); const out = new Set(); let cur = byId[currentId]
    while (cur) { out.add(cur.parent_id); cur = byId[cur.parent_id] }
    return out
  }, [tree, currentId])
  const toggle = (id) => setCollapsed((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })
  const render = (parentKey, depth) => (nodes[parentKey] || []).map((f) => {
    const kids = nodes[f.id]?.length > 0
    const open = kids && (!collapsed.has(f.id) || ancestors.has(f.id))
    return (
      <div key={f.id}>
        <div {...dropProps(f.id)} style={{ paddingLeft: `${8 + depth * 14}px` }}
          className={`flex items-center gap-1 rounded-lg pr-2 py-1 text-sm cursor-pointer ${currentId === f.id ? 'bg-ink-700 text-white' : 'text-ink-300 hover:bg-ink-800'} ${isTarget(f.id) ? 'ring-2 ring-brand-500 bg-brand-500/20' : ''}`}>
          <button onClick={(e) => { e.stopPropagation(); toggle(f.id) }} className={`w-4 h-4 grid place-items-center text-ink-500 ${kids ? '' : 'invisible'}`}>{open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}</button>
          <button onClick={() => onNavigate(f.id)} className="flex items-center gap-1.5 min-w-0 flex-1 text-left">
            {currentId === f.id ? <FolderOpen size={15} className="text-amber-300 shrink-0" /> : <Folder size={15} className="text-amber-300 shrink-0" />}<span className="truncate">{f.name}</span>
          </button>
        </div>
        {open && render(f.id, depth + 1)}
      </div>
    )
  })
  return (
    <div className="rounded-xl bg-ink-900 border border-ink-800 p-2 space-y-0.5">
      <div {...dropProps(null)} onClick={() => onNavigate(null)}
        className={`flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-sm cursor-pointer ${currentId == null ? 'bg-ink-700 text-white' : 'text-ink-300 hover:bg-ink-800'} ${isTarget(null) ? 'ring-2 ring-brand-500 bg-brand-500/20' : ''}`}>
        <Home size={15} /> All files
      </div>
      {render('root', 0)}
      {tree.length === 0 && <div className="px-2 py-1 text-xs text-ink-500">No folders yet</div>}
    </div>
  )
}

function SortTh({ label, k, sort, onClick, className = '' }) {
  const active = sort.key === k
  return (
    <th className={`p-3 cursor-pointer hover:text-ink-200 ${className}`} onClick={() => onClick(k)}>
      <span className="inline-flex items-center gap-1">{label}{active && (sort.dir === 'asc' ? <ArrowUp size={12} /> : <ArrowDown size={12} />)}</span>
    </th>
  )
}

function NameModal({ title, initial = '', onClose, onSubmit }) {
  const [name, setName] = useState(initial)
  const [error, setError] = useState('')
  return (
    <Modal title={title} onClose={onClose}>
      <form onSubmit={async (e) => { e.preventDefault(); try { await onSubmit(name.trim()) } catch (err) { setError(err.message) } }} className="space-y-3">
        <input className="input" autoFocus value={name} onChange={(e) => setName(e.target.value)} />
        <Alert>{error}</Alert>
        <div className="flex justify-end gap-2"><button type="button" className="btn-ghost" onClick={onClose}>Cancel</button><button className="btn-primary">Save</button></div>
      </form>
    </Modal>
  )
}

function ZipModal({ items, onClose, onSubmit }) {
  const [name, setName] = useState(items.length === 1 ? items[0].file.name.replace(/\.[^.]+$/, '') : 'archive')
  const total = items.reduce((a, it) => a + it.file.size, 0)
  return (
    <Modal title="Upload as zip" onClose={onClose}>
      <form onSubmit={(e) => { e.preventDefault(); onSubmit(items, name.trim() || 'archive') }} className="space-y-3">
        <p className="text-sm text-ink-300">{items.length} file(s), {bytes(total)} total, streamed into one zip archive on the way to the channel. The archive is store-only (no compression), so it is as fast as a plain upload.</p>
        <div><label className="label">Archive name</label><div className="flex items-center gap-1"><input className="input" autoFocus value={name} onChange={(e) => setName(e.target.value)} /><span className="text-ink-400">.zip</span></div></div>
        <div className="flex justify-end gap-2"><button type="button" className="btn-ghost" onClick={onClose}>Cancel</button><button className="btn-primary">Start</button></div>
      </form>
    </Modal>
  )
}

function MoveModal({ tree, items, currentFolderId, onClose, onMove }) {
  const [target, setTarget] = useState(currentFolderId ?? null)
  const blocked = new Set()
  for (const id of items.folders) {
    const start = tree.findIndex((t) => t.id === id)
    if (start < 0) continue
    blocked.add(id)
    for (let i = start + 1; i < tree.length && tree[i].depth > tree[start].depth; i++) blocked.add(tree[i].id)
  }
  const count = items.files.length + items.folders.length
  return (
    <Modal title={`Move ${count} item${count === 1 ? '' : 's'}`} onClose={onClose}>
      <div className="space-y-3">
        <div className="max-h-80 overflow-auto rounded-lg border border-ink-700 divide-y divide-ink-800 text-sm">
          <button className={`w-full text-left px-3 py-2 flex items-center gap-2 hover:bg-ink-800 ${target === null ? 'bg-brand-500/15' : ''}`} onClick={() => setTarget(null)}><Home size={16} /> All files</button>
          {tree.map((t) => (
            <button key={t.id} disabled={blocked.has(t.id)} style={{ paddingLeft: `${12 + t.depth * 18}px` }}
              className={`w-full text-left pr-3 py-2 flex items-center gap-2 hover:bg-ink-800 disabled:opacity-40 disabled:cursor-not-allowed ${target === t.id ? 'bg-brand-500/15' : ''}`}
              onClick={() => setTarget(t.id)}><Folder size={16} className="text-amber-300" /> {t.name}</button>
          ))}
        </div>
        <p className="text-xs text-ink-400">Only the listing changes. Nothing is re-uploaded or moved in the Telegram channel.</p>
        <div className="flex justify-end gap-2"><button className="btn-ghost" onClick={onClose}>Cancel</button><button className="btn-primary" onClick={() => onMove(target)}>Move here</button></div>
      </div>
    </Modal>
  )
}

function FolderUploadModal({ items, root, onClose, onZip, onTree }) {
  const [mode, setMode] = useState('zip')
  const [name, setName] = useState(root)
  const total = items.reduce((a, it) => a + it.file.size, 0)
  const submit = (e) => { e.preventDefault(); if (mode === 'zip') onZip(items, name.trim() || root); else onTree(items, root) }
  return (
    <Modal title={`Upload folder "${root}"`} onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        <p className="text-sm text-ink-300">{items.length} file(s), {bytes(total)} total, subfolders included.</p>
        <label className={`block rounded-lg border p-3 cursor-pointer ${mode === 'zip' ? 'border-brand-500 bg-brand-500/10' : 'border-ink-700'}`}>
          <div className="flex items-center gap-2 text-sm font-medium"><input type="radio" checked={mode === 'zip'} onChange={() => setMode('zip')} /> One zip archive</div>
          <p className="text-xs text-ink-400 mt-1">Folder structure is kept inside the archive, which is streamed to the channel as it is built. One entry in your file list, one download. Best for backups.</p>
          {mode === 'zip' && (
            <div className="mt-2 flex items-center gap-1"><input className="input" value={name} onChange={(e) => setName(e.target.value)} /><span className="text-ink-400">.zip</span></div>
          )}
        </label>
        <label className={`block rounded-lg border p-3 cursor-pointer ${mode === 'tree' ? 'border-brand-500 bg-brand-500/10' : 'border-ink-700'}`}>
          <div className="flex items-center gap-2 text-sm font-medium"><input type="radio" checked={mode === 'tree'} onChange={() => setMode('tree')} /> Individual files, keep folders</div>
          <p className="text-xs text-ink-400 mt-1">Recreates the folder tree here so you can browse and download files one by one. Each file becomes its own channel message.</p>
        </label>
        <div className="flex justify-end gap-2"><button type="button" className="btn-ghost" onClick={onClose}>Cancel</button><button className="btn-primary">Start</button></div>
      </form>
    </Modal>
  )
}

function Integrity({ f }) {
  if (f.verifying) return <span className="w-3.5 h-3.5 border-2 border-brand-400 border-t-transparent rounded-full animate-spin shrink-0" title="Verifying…" />
  if (f.integrity_error) return <ShieldAlert size={14} className="text-red-400 shrink-0" title={f.integrity_error} />
  if (f.verified_at) return <ShieldCheck size={14} className="text-emerald-400 shrink-0" title={`Verified ${when(f.verified_at)}`} />
  if (f.status === 'ready' && f.sha256) return <ShieldQuestion size={14} className="text-ink-600 shrink-0" title="Hashed at upload, not yet verified against the channel" />
  return null
}

function ImportModal({ folderId, onClose, onDone }) {
  const [path, setPath] = useState('')
  const [listing, setListing] = useState(null)
  const [pick, setPick] = useState(null)   // entry object
  const [mode, setMode] = useState('zip')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const load = async (p) => {
    setError('')
    try { const d = await get(`/api/admin/import/browse?path=${encodeURIComponent(p)}`); setListing(d); setPath(d.path || ''); setPick(null) } catch (e) { setError(e.message) }
  }
  useEffect(() => { load('') }, [])
  const crumbs = path ? path.split('/') : []
  const go = (i) => load(crumbs.slice(0, i).join('/'))
  const start = async () => {
    if (!pick) return
    setBusy(true); setError('')
    const rel = path ? `${path}/${pick.name}` : pick.name
    try {
      const r = await post('/api/admin/import', { path: rel, mode: pick.is_dir ? mode : 'file', folder_id: folderId })
      onDone(); alert(`${r.count} item(s) queued`)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }
  return (
    <Modal title="Import from server" onClose={onClose}>
      {listing && !listing.enabled ? (
        <div className="text-sm text-ink-300 space-y-2">
          <p>No import directory is mounted.</p>
          <p className="text-xs text-ink-400">Mount a host directory at <code className="bg-ink-800 px-1 rounded">/import</code> in docker-compose.yml (or set <code className="bg-ink-800 px-1 rounded">OTS_IMPORT_DIR</code>) and restart. Files are read in place and never deleted.</p>
        </div>
      ) : (
        <div className="space-y-3">
          <nav className="flex items-center gap-1 text-xs text-ink-400 flex-wrap">
            <button className="hover:text-white" onClick={() => go(0)}>{listing?.root || 'import'}</button>
            {crumbs.map((c, i) => <span key={i} className="flex items-center gap-1"><ChevronRight size={12} /><button className="hover:text-white" onClick={() => go(i + 1)}>{c}</button></span>)}
          </nav>
          <div className="max-h-64 overflow-auto rounded-lg border border-ink-700 divide-y divide-ink-800 text-sm">
            {listing?.entries.length === 0 && <div className="px-3 py-2 text-ink-400">Empty directory</div>}
            {(listing?.entries || []).map((e) => (
              <div key={e.name} className={`flex items-center gap-2 px-3 py-1.5 cursor-pointer hover:bg-ink-800 ${pick?.name === e.name ? 'bg-brand-500/15' : ''}`}
                onClick={() => setPick(e)} onDoubleClick={() => e.is_dir && load(path ? `${path}/${e.name}` : e.name)}>
                {e.is_dir ? <Folder size={16} className="text-amber-300 shrink-0" /> : fileIcon(e.name)}
                <span className="truncate flex-1">{e.name}</span>
                <span className="text-xs text-ink-400 shrink-0">{e.is_dir ? 'folder' : bytes(e.size)}</span>
              </div>
            ))}
          </div>
          {pick?.is_dir && (
            <div className="flex gap-4 text-sm">
              <label className="flex items-center gap-2"><input type="radio" checked={mode === 'zip'} onChange={() => setMode('zip')} /> One zip archive</label>
              <label className="flex items-center gap-2"><input type="radio" checked={mode === 'tree'} onChange={() => setMode('tree')} /> Keep folder tree</label>
            </div>
          )}
          <p className="text-xs text-ink-400">Double-click a folder to open it, single-click to select. Sources are read in place and never deleted. Imports go into the folder you are viewing.</p>
          <Alert>{error}</Alert>
          <div className="flex justify-end gap-2"><button className="btn-ghost" onClick={onClose}>Cancel</button><button className="btn-primary" disabled={!pick || busy} onClick={start}>{busy ? 'Starting…' : `Import ${pick ? pick.name : ''}`}</button></div>
        </div>
      )}
    </Modal>
  )
}

export function ShareModal({ file, onClose }) {
  const [list, setList] = useState(null)
  const [form, setForm] = useState({ label: '', expires_in_hours: '', max_downloads: '', password: '' })
  const [error, setError] = useState('')
  const [copied, setCopied] = useState('')
  const load = () => get(`/api/files/${file.id}/shares`).then(setList).catch((e) => setError(e.message))
  useEffect(() => { load() }, [])  // eslint-disable-line react-hooks/exhaustive-deps
  const create = async (e) => {
    e.preventDefault(); setError('')
    try {
      await post(`/api/files/${file.id}/shares`, {
        label: form.label || null, expires_in_hours: form.expires_in_hours ? Number(form.expires_in_hours) : null,
        max_downloads: form.max_downloads ? Number(form.max_downloads) : null, password: form.password || null,
      })
      setForm({ label: '', expires_in_hours: '', max_downloads: '', password: '' }); load()
    } catch (err) { setError(err.message) }
  }
  const copy = async (s) => { try { await navigator.clipboard.writeText(s.url); setCopied(s.id); setTimeout(() => setCopied(''), 1500) } catch { prompt('Copy the link:', s.url) } }
  const toggle = async (s) => { try { await post(`/api/shares/${s.id}/toggle`); load() } catch (err) { setError(err.message) } }
  const remove = async (s) => { if (!confirm('Delete this link? Anyone holding it loses access.')) return; try { await del(`/api/shares/${s.id}`); load() } catch (err) { setError(err.message) } }
  return (
    <Modal title={`Share "${file.name}"`} onClose={onClose}>
      <div className="space-y-4">
        <div className="space-y-2">
          {list === null && <div className="text-sm text-ink-400">Loading…</div>}
          {list?.length === 0 && <div className="text-sm text-ink-400">No links yet. Create one below.</div>}
          {(list || []).map((s) => (
            <div key={s.id} className={`rounded-lg border p-2 text-sm ${s.active ? 'border-ink-700' : 'border-ink-800 opacity-60'}`}>
              <div className="flex items-center gap-2">
                <code className="flex-1 truncate text-xs text-ink-300">{s.url}</code>
                <button className="btn-ghost" onClick={() => copy(s)} title="Copy link">{copied === s.id ? 'Copied' : <Copy size={14} />}</button>
                <button className="btn-ghost" onClick={() => toggle(s)}>{s.disabled ? 'Enable' : 'Disable'}</button>
                <button className="btn-ghost" onClick={() => remove(s)} title="Delete"><Trash size={14} /></button>
              </div>
              <div className="text-xs text-ink-400 mt-1">
                {s.label && <span>{s.label} · </span>}
                {s.download_count} download{s.download_count === 1 ? '' : 's'}{s.max_downloads != null && ` of ${s.max_downloads}`}
                {s.expires_at && ` · expires ${when(s.expires_at)}`}{s.has_password && ' · password'}{s.disabled && ' · disabled'}
                {!s.active && !s.disabled && ' · no longer active'}
              </div>
            </div>
          ))}
        </div>
        <form onSubmit={create} className="space-y-2 border-t border-ink-800 pt-3">
          <div className="text-xs uppercase tracking-wide text-ink-400">New link</div>
          <div className="grid grid-cols-2 gap-2">
            <input className="input" placeholder="label (optional)" value={form.label} onChange={(e) => setForm({ ...form, label: e.target.value })} />
            <input className="input" type="password" placeholder="password (optional)" autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
            <input className="input" type="number" min="1" placeholder="expires in hours (optional)" value={form.expires_in_hours} onChange={(e) => setForm({ ...form, expires_in_hours: e.target.value })} />
            <input className="input" type="number" min="1" placeholder="max downloads (optional)" value={form.max_downloads} onChange={(e) => setForm({ ...form, max_downloads: e.target.value })} />
          </div>
          <p className="text-xs text-ink-400">Downloads stream through this server from the channel, so the link works only while the server is reachable at that address.</p>
          <Alert>{error}</Alert>
          <div className="flex justify-end gap-2"><button type="button" className="btn-ghost" onClick={onClose}>Close</button><button className="btn-primary"><Link2 size={16} /> Create link</button></div>
        </form>
      </div>
    </Modal>
  )
}
