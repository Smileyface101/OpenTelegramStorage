import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams, Link } from 'react-router-dom'
import { Folder, FolderPlus, FolderUp, Upload, Download, Trash2, Pencil, Archive, RefreshCw, ChevronRight, FileIcon, Search, FolderInput, ArrowUp, ArrowDown, X } from 'lucide-react'
import { get, post, del, patch } from '../lib/api'
import { uploadFile, uploadBundle, uploadTree, itemsFromFileList, itemsFromDataTransfer, rootFolderName } from '../lib/uploader'
import { bytes, when, pct } from '../lib/format'
import { Modal, Alert, Progress, StatusBadge } from '../components/ui'

const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`)

export default function Files() {
  const [params, setParams] = useSearchParams()
  const folderId = params.get('folder') ? Number(params.get('folder')) : null
  const [data, setData] = useState(null)
  const [q, setQ] = useState('')
  const [error, setError] = useState('')
  const [uploads, setUploads] = useState([])   // browser -> server in flight
  const [modal, setModal] = useState(null)      // {type:'folder'} | {type:'rename', file} | {type:'zip', files}
  const fileInput = useRef(null)
  const dirInput = useRef(null)
  const [dragging, setDragging] = useState(false)
  const [selected, setSelected] = useState({ files: new Set(), folders: new Set() })
  const [sort, setSort] = useState(() => { try { return JSON.parse(localStorage.getItem('ots.sort')) || { key: 'created_at', dir: 'desc' } } catch { return { key: 'created_at', dir: 'desc' } } })
  const [dropTarget, setDropTarget] = useState(null)   // folder id (or 'root') highlighted during an internal drag
  const internalDrag = useRef(null)                     // {files:[], folders:[]} while dragging rows

  const load = useCallback(async () => {
    try {
      const qs = new URLSearchParams()
      if (folderId != null) qs.set('folder_id', folderId)
      if (q) qs.set('q', q)
      setData(await get(`/api/files?${qs}`))
    } catch (e) { setError(e.message) }
  }, [folderId, q])

  useEffect(() => { load(); setSelected({ files: new Set(), folders: new Set() }) }, [load])
  useEffect(() => { try { localStorage.setItem('ots.sort', JSON.stringify(sort)) } catch { /* ignore */ } }, [sort])

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

  const toggleSel = (kind, id) => setSelected((s) => { const n = new Set(s[kind]); n.has(id) ? n.delete(id) : n.add(id); return { ...s, [kind]: n } })
  const selCount = selected.files.size + selected.folders.size
  const allIds = { files: (data?.files || []).map((f) => f.id), folders: (data?.folders || []).map((d) => d.id) }
  const allSelected = data && selCount > 0 && selCount === allIds.files.length + allIds.folders.length
  const toggleAll = () => setSelected(allSelected ? { files: new Set(), folders: new Set() } : { files: new Set(allIds.files), folders: new Set(allIds.folders) })

  const moveItems = async ({ files, folders }, targetFolderId) => {
    if (!files.length && !folders.length) return
    if (folders.includes(targetFolderId)) return
    try {
      await post('/api/move', { file_ids: files, folder_ids: folders, target_folder_id: targetFolderId })
      setSelected({ files: new Set(), folders: new Set() })
      setModal(null)
      await load()
    } catch (e) { setError(e.message) }
  }

  // ---- row drag-and-drop (internal) ----
  const onRowDragStart = (e, kind, id) => {
    const inSel = selected[kind].has(id)
    const payload = inSel
      ? { files: [...selected.files], folders: [...selected.folders] }
      : { files: kind === 'files' ? [id] : [], folders: kind === 'folders' ? [id] : [] }
    internalDrag.current = payload
    e.dataTransfer.setData('application/x-ots-move', '1')
    e.dataTransfer.effectAllowed = 'move'
  }
  const isInternal = (e) => Array.from(e.dataTransfer.types || []).includes('application/x-ots-move')
  const isExternal = (e) => Array.from(e.dataTransfer.types || []).includes('Files')
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
    // Files dragged in from the desktop: upload straight into that folder.
    e.preventDefault(); e.stopPropagation(); setDropTarget(null); setDragging(false)
    try {
      const { items, hadDirectory } = await itemsFromDataTransfer(e.dataTransfer)
      if (hadDirectory) startFolder(items, targetFolderId)
      else startUploads(items.map((it) => it.file), false, targetFolderId)
    } catch (err) { setError(err.message) }
  }
  const onRowDragEnd = () => { internalDrag.current = null; setDropTarget(null) }
  const pending = data?.files.some((f) => f.status !== 'ready' && f.status !== 'failed')
  useEffect(() => {
    if (!pending) return
    const t = setInterval(load, 2000)
    return () => clearInterval(t)
  }, [pending, load])

  const startUploads = (files, asZip = false, target = folderId) => {
    if (!files.length) return
    if (asZip) return setModal({ type: 'zip', items: files.map((file) => ({ file, path: file.name })), target })
    files.forEach((f) => runUpload(f, target))
  }

  const startFolder = (items, target = folderId) => {
    if (!items.length) { setError('The selected folder contains no files (or the browser did not grant access to it).'); return }
    setModal({ type: 'folder-upload', items, root: rootFolderName(items) || 'folder', target })
  }

  const onPickFolder = (e) => {
    try {
      const list = e.target.files
      console.info('[ots] folder picker returned', list?.length, 'files')
      startFolder(itemsFromFileList(list))
    } catch (err) {
      console.error('[ots] folder picker failed', err)
      setError(`Could not read the folder: ${err.message}`)
    } finally { e.target.value = '' }
  }

  const openFolderPicker = () => {
    const el = dirInput.current
    if (!el || !('webkitdirectory' in el)) { setError('This browser cannot pick folders. Drag the folder onto the page instead, or zip it and upload the file.'); return }
    el.click()
  }

  const track = (name, size) => {
    const id = uuid()
    const ctrl = new AbortController()
    setUploads((u) => [...u, { id, name, size, done: 0, ctrl }])
    const onProgress = (d) => setUploads((u) => u.map((x) => x.id === id ? { ...x, done: d } : x))
    const finish = () => setUploads((u) => u.filter((x) => x.id !== id))
    return { ctrl, onProgress, finish }
  }

  const runUpload = async (file, target = folderId) => {
    const t = track(file.name, file.size)
    try {
      await uploadFile(file, { folderId: target, signal: t.ctrl.signal, onProgress: t.onProgress })
      await load()
    } catch (e) {
      if (!t.ctrl.signal.aborted) setError(`${file.name}: ${e.message}`)
    } finally { t.finish() }
  }

  const runZip = async (items, name, compress, target = folderId) => {
    setModal(null)
    const size = items.reduce((a, it) => a + it.file.size, 0)
    const t = track(`${name}.zip`, size)
    try {
      await uploadBundle(items, { name, folderId: target, compress, signal: t.ctrl.signal, onProgress: t.onProgress })
      await load()
    } catch (e) {
      if (!t.ctrl.signal.aborted) setError(`${name}.zip: ${e.message}`)
    } finally { t.finish() }
  }

  const runTree = async (items, root, target = folderId) => {
    setModal(null)
    const size = items.reduce((a, it) => a + it.file.size, 0)
    const t = track(`${root}/ (${items.length} files)`, size)
    try {
      await uploadTree(items, { folderId: target, signal: t.ctrl.signal, onProgress: t.onProgress, onFileDone: load })
      await load()
    } catch (e) {
      if (!t.ctrl.signal.aborted) setError(`${root}: ${e.message}`)
    } finally { t.finish() }
  }

  const onDrop = async (e) => {
    if (isInternal(e)) { e.preventDefault(); setDragging(false); return }
    e.preventDefault(); setDragging(false)
    try {
      const { items, hadDirectory } = await itemsFromDataTransfer(e.dataTransfer)
      console.info('[ots] drop:', items.length, 'files, directory =', hadDirectory)
      if (hadDirectory) startFolder(items)
      else startUploads(items.map((it) => it.file))
    } catch (err) { setError(err.message) }
  }

  const removeFile = async (f) => {
    if (!confirm(`Delete "${f.name}" from the channel? This cannot be undone.`)) return
    try { await del(`/api/files/${f.id}`); await load() } catch (e) { setError(e.message) }
  }
  const removeFolder = async (d) => {
    if (!confirm(`Delete folder "${d.name}" and everything in it?`)) return
    try { await del(`/api/folders/${d.id}`); await load() } catch (e) { setError(e.message) }
  }
  const retry = async (f) => { try { await post(`/api/files/${f.id}/retry`); await load() } catch (e) { setError(e.message) } }

  return (
    <div className="space-y-4" onDragOver={(e) => { e.preventDefault(); if (!isInternal(e)) setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={onDrop}>
      <div className="flex flex-wrap items-center gap-2">
        <nav className="flex items-center gap-1 text-sm mr-auto">
          <Link className={`hover:underline rounded px-1 ${dropTarget === 'root' ? 'bg-brand-500/30 ring-1 ring-brand-500' : ''}`} to="/files"
            onDragOver={(e) => onTargetDragOver(e, 'root')} onDragLeave={() => setDropTarget(null)} onDrop={(e) => onTargetDrop(e, null)}>All files</Link>
          {(data?.breadcrumbs || []).map((c) => (
            <span key={c.id} className="flex items-center gap-1"><ChevronRight size={14} className="text-ink-500" />
              <Link className={`hover:underline rounded px-1 ${dropTarget === c.id ? 'bg-brand-500/30 ring-1 ring-brand-500' : ''}`} to={`/files?folder=${c.id}`}
                onDragOver={(e) => onTargetDragOver(e, c.id)} onDragLeave={() => setDropTarget(null)} onDrop={(e) => onTargetDrop(e, c.id)}>{c.name}</Link></span>
          ))}
        </nav>
        <div className="relative">
          <Search size={14} className="absolute left-2 top-2.5 text-ink-400" />
          <input className="input pl-7 w-48" placeholder="Search" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <button className="btn-ghost" onClick={() => setModal({ type: 'folder' })}><FolderPlus size={16} /> Folder</button>
        <button className="btn-ghost" title="Pick several files; they are zipped into one archive on the server before going to Telegram" onClick={() => { fileInput.current.dataset.zip = '1'; fileInput.current.click() }}><Archive size={16} /> Files as zip</button>
        <button className="btn-ghost" title="Pick a whole folder (all subfolders included)" onClick={openFolderPicker}><FolderUp size={16} /> Upload folder</button>
        <button className="btn-primary" onClick={() => { fileInput.current.dataset.zip = ''; fileInput.current.click() }}><Upload size={16} /> Upload files</button>
        <input ref={fileInput} type="file" multiple hidden onChange={(e) => { startUploads(Array.from(e.target.files), e.target.dataset.zip === '1'); e.target.value = '' }} />
        <input ref={dirInput} type="file" webkitdirectory="" directory="" multiple hidden onChange={onPickFolder} />
      </div>

      {selCount > 0 && (
        <div className="flex items-center gap-2 rounded-lg bg-brand-500/10 border border-brand-500/30 px-3 py-2 text-sm">
          <span>{selCount} selected</span>
          <button className="btn-ghost" onClick={() => setModal({ type: 'move', items: { files: [...selected.files], folders: [...selected.folders] } })}><FolderInput size={16} /> Move to…</button>
          <span className="text-xs text-ink-400 hidden sm:inline">or drag the rows onto a folder</span>
          <button className="ml-auto text-ink-400 hover:text-white" onClick={() => setSelected({ files: new Set(), folders: new Set() })}><X size={16} /></button>
        </div>
      )}
      <Alert>{error && <span className="flex justify-between">{error}<button onClick={() => setError('')}>✕</button></span>}</Alert>

      {uploads.length > 0 && (
        <div className="card space-y-3">
          <div className="text-xs uppercase tracking-wide text-ink-400">Uploading to server</div>
          {uploads.map((u) => (
            <div key={u.id}>
              <div className="flex justify-between text-sm"><span className="truncate">{u.name}</span>
                <span className="text-ink-400 flex items-center gap-2">{bytes(u.done)} / {bytes(u.size)}
                  <button onClick={() => u.ctrl.abort()} className="text-red-300 hover:text-red-200">cancel</button></span></div>
              <Progress value={pct(u.done, u.size)} className="mt-1" />
            </div>
          ))}
        </div>
      )}

      <div className={`card p-0 overflow-hidden ${dragging ? 'ring-2 ring-brand-500' : ''}`}>
        {!data ? <div className="p-6 text-sm text-ink-400">Loading…</div> : (
          <table className="w-full text-sm">
            <thead className="text-xs uppercase text-ink-400 bg-ink-800/50 select-none">
              <tr>
                <th className="p-3 w-8"><input type="checkbox" checked={!!allSelected} onChange={toggleAll} disabled={!data || (allIds.files.length + allIds.folders.length) === 0} /></th>
                <SortTh label="Name" k="name" sort={sort} onClick={toggleSort} className="text-left" />
                <SortTh label="Size" k="size" sort={sort} onClick={toggleSort} className="text-right hidden sm:table-cell" />
                <SortTh label="Status" k="status" sort={sort} onClick={toggleSort} className="text-left hidden md:table-cell" />
                <SortTh label="Added" k="created_at" sort={sort} onClick={toggleSort} className="text-left hidden lg:table-cell" />
                <th className="p-3" />
              </tr>
            </thead>
            <tbody>
              {sortedFolders.map((d) => (
                <tr key={`d${d.id}`} draggable onDragStart={(e) => onRowDragStart(e, 'folders', d.id)} onDragEnd={onRowDragEnd}
                  onDragOver={(e) => onTargetDragOver(e, d.id)} onDragLeave={() => setDropTarget(null)} onDrop={(e) => onTargetDrop(e, d.id)}
                  className={`border-t border-ink-800 hover:bg-ink-800/40 ${selected.folders.has(d.id) ? 'bg-brand-500/10' : ''} ${dropTarget === d.id ? 'bg-brand-500/20 ring-1 ring-inset ring-brand-500' : ''}`}>
                  <td className="p-3"><input type="checkbox" checked={selected.folders.has(d.id)} onChange={() => toggleSel('folders', d.id)} /></td>
                  <td className="p-3"><Link to={`/files?folder=${d.id}`} className="flex items-center gap-2"><Folder size={16} className="text-amber-300" />{d.name}{dropTarget === d.id && <span className="text-xs text-brand-400 ml-2">drop here</span>}</Link></td>
                  <td className="hidden sm:table-cell" /><td className="hidden md:table-cell" /><td className="p-3 text-ink-400 hidden lg:table-cell">{when(d.created_at)}</td>
                  <td className="p-3">
                    <div className="flex justify-end gap-2 text-ink-400">
                      <button onClick={() => setModal({ type: 'move', items: { files: [], folders: [d.id] } })} className="hover:text-white" title="Move"><FolderInput size={16} /></button>
                      <button onClick={() => removeFolder(d)} className="hover:text-red-300" title="Delete"><Trash2 size={16} /></button>
                    </div>
                  </td>
                </tr>
              ))}
              {sortedFiles.map((f) => (
                <tr key={f.id} draggable onDragStart={(e) => onRowDragStart(e, 'files', f.id)} onDragEnd={onRowDragEnd}
                  className={`border-t border-ink-800 hover:bg-ink-800/40 ${selected.files.has(f.id) ? 'bg-brand-500/10' : ''}`}>
                  <td className="p-3"><input type="checkbox" checked={selected.files.has(f.id)} onChange={() => toggleSel('files', f.id)} /></td>
                  <td className="p-3">
                    <div className="flex items-center gap-2 min-w-0">{f.is_archive ? <Archive size={16} className="text-violet-300 shrink-0" /> : <FileIcon size={16} className="text-ink-400 shrink-0" />}
                      <span className="truncate">{f.name}</span>
                      {f.parts_total > 1 && <span className="text-xs text-ink-400">{f.parts_total} parts</span>}</div>
                    {f.status !== 'ready' && f.status !== 'failed' && <Progress value={pct(f.bytes_done, f.size)} className="mt-1 max-w-xs" />}
                    {f.error && <div className="text-xs text-red-300 mt-1 truncate">{f.error}</div>}
                  </td>
                  <td className="p-3 text-right text-ink-300 hidden sm:table-cell">{bytes(f.size)}</td>
                  <td className="p-3 hidden md:table-cell"><StatusBadge status={f.status} /></td>
                  <td className="p-3 text-ink-400 hidden lg:table-cell">{when(f.created_at)}</td>
                  <td className="p-3">
                    <div className="flex justify-end gap-2 text-ink-400">
                      {f.status === 'ready' && <a href={`/api/files/${f.id}/download`} className="hover:text-white" title="Download"><Download size={16} /></a>}
                      {f.status === 'failed' && <button onClick={() => retry(f)} className="hover:text-white" title="Retry"><RefreshCw size={16} /></button>}
                      <button onClick={() => setModal({ type: 'move', items: { files: [f.id], folders: [] } })} className="hover:text-white" title="Move"><FolderInput size={16} /></button>
                      <button onClick={() => setModal({ type: 'rename', file: f })} className="hover:text-white" title="Rename"><Pencil size={16} /></button>
                      <button onClick={() => removeFile(f)} className="hover:text-red-300" title="Delete"><Trash2 size={16} /></button>
                    </div>
                  </td>
                </tr>
              ))}
              {data.folders.length === 0 && data.files.length === 0 && (
                <tr><td colSpan={6} className="p-10 text-center text-ink-400">Drop files or folders here, or onto a folder row to upload into it. Files larger than the part size are split into parts automatically.</td></tr>
              )}
            </tbody>
          </table>
        )}
      </div>

      {modal?.type === 'folder' && <NameModal title="New folder" onClose={() => setModal(null)} onSubmit={async (name) => { await post('/api/folders', { name, parent_id: folderId }); setModal(null); load() }} />}
      {modal?.type === 'rename' && <NameModal title="Rename" initial={modal.file.name} onClose={() => setModal(null)} onSubmit={async (name) => { await patch(`/api/files/${modal.file.id}`, { name }); setModal(null); load() }} />}
      {modal?.type === 'zip' && <ZipModal items={modal.items} onClose={() => setModal(null)} onSubmit={(items, name, compress) => runZip(items, name, compress, modal.target)} />}
      {modal?.type === 'move' && <MoveModal items={modal.items} currentFolderId={folderId} onClose={() => setModal(null)} onMove={(target) => moveItems(modal.items, target)} />}
      {modal?.type === 'folder-upload' && <FolderUploadModal items={modal.items} root={modal.root} onClose={() => setModal(null)} onZip={(items, name, compress) => runZip(items, name, compress, modal.target)} onTree={(items, root) => runTree(items, root, modal.target)} />}
    </div>
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
  const [compress, setCompress] = useState(false)
  const total = items.reduce((a, it) => a + it.file.size, 0)
  return (
    <Modal title="Upload as zip" onClose={onClose}>
      <form onSubmit={(e) => { e.preventDefault(); onSubmit(items, name.trim() || 'archive', compress) }} className="space-y-3">
        <p className="text-sm text-ink-300">{items.length} file(s), {bytes(total)} total, will be zipped on the server and then sent to the channel.</p>
        <div><label className="label">Archive name</label><div className="flex items-center gap-1"><input className="input" value={name} onChange={(e) => setName(e.target.value)} /><span className="text-ink-400">.zip</span></div></div>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={compress} onChange={(e) => setCompress(e.target.checked)} /> Compress (slower; pointless for media and already-compressed files)</label>
        <div className="flex justify-end gap-2"><button type="button" className="btn-ghost" onClick={onClose}>Cancel</button><button className="btn-primary">Start</button></div>
      </form>
    </Modal>
  )
}

function FolderUploadModal({ items, root, onClose, onZip, onTree }) {
  const [mode, setMode] = useState('zip')
  const [name, setName] = useState(root)
  const [compress, setCompress] = useState(false)
  const total = items.reduce((a, it) => a + it.file.size, 0)
  const submit = (e) => {
    e.preventDefault()
    if (mode === 'zip') onZip(items, name.trim() || root, compress)
    else onTree(items, root)
  }
  return (
    <Modal title={`Upload folder "${root}"`} onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        <p className="text-sm text-ink-300">{items.length} file(s), {bytes(total)} total, subfolders included.</p>
        <label className={`block rounded-lg border p-3 cursor-pointer ${mode === 'zip' ? 'border-brand-500 bg-brand-500/10' : 'border-ink-700'}`}>
          <div className="flex items-center gap-2 text-sm font-medium"><input type="radio" checked={mode === 'zip'} onChange={() => setMode('zip')} /> One zip archive</div>
          <p className="text-xs text-ink-400 mt-1">Folder structure is kept inside the archive. One entry in your file list, fewer channel messages, and you download it back as a single zip. Best for backups.</p>
          {mode === 'zip' && (
            <div className="mt-2 space-y-2">
              <div className="flex items-center gap-1"><input className="input" value={name} onChange={(e) => setName(e.target.value)} /><span className="text-ink-400">.zip</span></div>
              <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={compress} onChange={(e) => setCompress(e.target.checked)} /> Compress (slower; pointless for media)</label>
            </div>
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

function SortTh({ label, k, sort, onClick, className = '' }) {
  const active = sort.key === k
  return (
    <th className={`p-3 cursor-pointer hover:text-ink-200 ${className}`} onClick={() => onClick(k)}>
      <span className="inline-flex items-center gap-1">{label}{active && (sort.dir === 'asc' ? <ArrowUp size={12} /> : <ArrowDown size={12} />)}</span>
    </th>
  )
}

function MoveModal({ items, currentFolderId, onClose, onMove }) {
  const [tree, setTree] = useState(null)
  const [target, setTarget] = useState(currentFolderId ?? null)
  const [error, setError] = useState('')
  useEffect(() => { get('/api/folders/tree').then(setTree).catch((e) => setError(e.message)) }, [])
  // A folder cannot be moved into itself or its own subtree: hide those.
  const blocked = new Set()
  if (tree) {
    for (const id of items.folders) {
      const start = tree.findIndex((t) => t.id === id)
      if (start < 0) continue
      blocked.add(id)
      for (let i = start + 1; i < tree.length && tree[i].depth > tree[start].depth; i++) blocked.add(tree[i].id)
    }
  }
  const count = items.files.length + items.folders.length
  return (
    <Modal title={`Move ${count} item${count === 1 ? '' : 's'}`} onClose={onClose}>
      <div className="space-y-3">
        <div className="max-h-80 overflow-auto rounded-lg border border-ink-700 divide-y divide-ink-800 text-sm">
          <button className={`w-full text-left px-3 py-2 flex items-center gap-2 hover:bg-ink-800 ${target === null ? 'bg-brand-500/15' : ''}`} onClick={() => setTarget(null)}>
            <Folder size={16} className="text-amber-300" /> All files
          </button>
          {tree === null && <div className="px-3 py-2 text-ink-400">Loading…</div>}
          {(tree || []).map((t) => (
            <button key={t.id} disabled={blocked.has(t.id)} style={{ paddingLeft: `${12 + t.depth * 18}px` }}
              className={`w-full text-left pr-3 py-2 flex items-center gap-2 hover:bg-ink-800 disabled:opacity-40 disabled:cursor-not-allowed ${target === t.id ? 'bg-brand-500/15' : ''}`}
              onClick={() => setTarget(t.id)}>
              <Folder size={16} className="text-amber-300" /> {t.name}
            </button>
          ))}
        </div>
        <p className="text-xs text-ink-400">Only the listing changes. Nothing is re-uploaded or moved in the Telegram channel.</p>
        <Alert>{error}</Alert>
        <div className="flex justify-end gap-2"><button className="btn-ghost" onClick={onClose}>Cancel</button><button className="btn-primary" onClick={() => onMove(target)}>Move here</button></div>
      </div>
    </Modal>
  )
}
