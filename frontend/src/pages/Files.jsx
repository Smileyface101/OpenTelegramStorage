import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams, Link } from 'react-router-dom'
import { Folder, FolderPlus, Upload, Download, Trash2, Pencil, Archive, RefreshCw, ChevronRight, FileIcon, Search } from 'lucide-react'
import { get, post, del, patch } from '../lib/api'
import { uploadFile, uploadBundle } from '../lib/uploader'
import { bytes, when, pct } from '../lib/format'
import { Modal, Alert, Progress, StatusBadge } from '../components/ui'

export default function Files() {
  const [params, setParams] = useSearchParams()
  const folderId = params.get('folder') ? Number(params.get('folder')) : null
  const [data, setData] = useState(null)
  const [q, setQ] = useState('')
  const [error, setError] = useState('')
  const [uploads, setUploads] = useState([])   // browser -> server in flight
  const [modal, setModal] = useState(null)      // {type:'folder'} | {type:'rename', file} | {type:'zip', files}
  const fileInput = useRef(null)
  const [dragging, setDragging] = useState(false)

  const load = useCallback(async () => {
    try {
      const qs = new URLSearchParams()
      if (folderId != null) qs.set('folder_id', folderId)
      if (q) qs.set('q', q)
      setData(await get(`/api/files?${qs}`))
    } catch (e) { setError(e.message) }
  }, [folderId, q])

  useEffect(() => { load() }, [load])
  const pending = data?.files.some((f) => f.status !== 'ready' && f.status !== 'failed')
  useEffect(() => {
    if (!pending) return
    const t = setInterval(load, 2000)
    return () => clearInterval(t)
  }, [pending, load])

  const startUploads = (files, asZip = false) => {
    if (!files.length) return
    if (asZip) return setModal({ type: 'zip', files })
    files.forEach((f) => runUpload(f))
  }

  const runUpload = async (file) => {
    const id = crypto.randomUUID()
    const ctrl = new AbortController()
    setUploads((u) => [...u, { id, name: file.name, size: file.size, done: 0, ctrl }])
    try {
      await uploadFile(file, { folderId, signal: ctrl.signal, onProgress: (d) => setUploads((u) => u.map((x) => x.id === id ? { ...x, done: d } : x)) })
      await load()
    } catch (e) {
      if (!ctrl.signal.aborted) setError(`${file.name}: ${e.message}`)
    } finally {
      setUploads((u) => u.filter((x) => x.id !== id))
    }
  }

  const runZip = async (files, name, compress) => {
    setModal(null)
    const id = crypto.randomUUID()
    const ctrl = new AbortController()
    const size = files.reduce((a, f) => a + f.size, 0)
    setUploads((u) => [...u, { id, name: `${name}.zip`, size, done: 0, ctrl }])
    try {
      await uploadBundle(files, { name, folderId, compress, signal: ctrl.signal, onProgress: (d) => setUploads((u) => u.map((x) => x.id === id ? { ...x, done: d } : x)) })
      await load()
    } catch (e) {
      if (!ctrl.signal.aborted) setError(`${name}.zip: ${e.message}`)
    } finally {
      setUploads((u) => u.filter((x) => x.id !== id))
    }
  }

  const onDrop = (e) => {
    e.preventDefault(); setDragging(false)
    startUploads(Array.from(e.dataTransfer.files))
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
    <div className="space-y-4" onDragOver={(e) => { e.preventDefault(); setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={onDrop}>
      <div className="flex flex-wrap items-center gap-2">
        <nav className="flex items-center gap-1 text-sm mr-auto">
          <Link className="hover:underline" to="/files">All files</Link>
          {(data?.breadcrumbs || []).map((c) => (
            <span key={c.id} className="flex items-center gap-1"><ChevronRight size={14} className="text-ink-500" /><Link className="hover:underline" to={`/files?folder=${c.id}`}>{c.name}</Link></span>
          ))}
        </nav>
        <div className="relative">
          <Search size={14} className="absolute left-2 top-2.5 text-ink-400" />
          <input className="input pl-7 w-48" placeholder="Search" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <button className="btn-ghost" onClick={() => setModal({ type: 'folder' })}><FolderPlus size={16} /> Folder</button>
        <button className="btn-ghost" onClick={() => { fileInput.current.dataset.zip = '1'; fileInput.current.click() }}><Archive size={16} /> Upload as zip</button>
        <button className="btn-primary" onClick={() => { fileInput.current.dataset.zip = ''; fileInput.current.click() }}><Upload size={16} /> Upload</button>
        <input ref={fileInput} type="file" multiple hidden onChange={(e) => { startUploads(Array.from(e.target.files), e.target.dataset.zip === '1'); e.target.value = '' }} />
      </div>

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
            <thead className="text-xs uppercase text-ink-400 bg-ink-800/50">
              <tr><th className="text-left p-3">Name</th><th className="text-right p-3 hidden sm:table-cell">Size</th><th className="text-left p-3 hidden md:table-cell">Status</th><th className="text-left p-3 hidden lg:table-cell">Added</th><th className="p-3" /></tr>
            </thead>
            <tbody>
              {data.folders.map((d) => (
                <tr key={`d${d.id}`} className="border-t border-ink-800 hover:bg-ink-800/40">
                  <td className="p-3"><Link to={`/files?folder=${d.id}`} className="flex items-center gap-2"><Folder size={16} className="text-amber-300" />{d.name}</Link></td>
                  <td className="hidden sm:table-cell" /><td className="hidden md:table-cell" /><td className="hidden lg:table-cell" />
                  <td className="p-3 text-right"><button onClick={() => removeFolder(d)} className="text-ink-400 hover:text-red-300"><Trash2 size={16} /></button></td>
                </tr>
              ))}
              {data.files.map((f) => (
                <tr key={f.id} className="border-t border-ink-800 hover:bg-ink-800/40">
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
                      <button onClick={() => setModal({ type: 'rename', file: f })} className="hover:text-white" title="Rename"><Pencil size={16} /></button>
                      <button onClick={() => removeFile(f)} className="hover:text-red-300" title="Delete"><Trash2 size={16} /></button>
                    </div>
                  </td>
                </tr>
              ))}
              {data.folders.length === 0 && data.files.length === 0 && (
                <tr><td colSpan={5} className="p-10 text-center text-ink-400">Drop files here or use Upload. Files larger than the part size are split automatically.</td></tr>
              )}
            </tbody>
          </table>
        )}
      </div>

      {modal?.type === 'folder' && <NameModal title="New folder" onClose={() => setModal(null)} onSubmit={async (name) => { await post('/api/folders', { name, parent_id: folderId }); setModal(null); load() }} />}
      {modal?.type === 'rename' && <NameModal title="Rename" initial={modal.file.name} onClose={() => setModal(null)} onSubmit={async (name) => { await patch(`/api/files/${modal.file.id}`, { name }); setModal(null); load() }} />}
      {modal?.type === 'zip' && <ZipModal files={modal.files} onClose={() => setModal(null)} onSubmit={runZip} />}
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

function ZipModal({ files, onClose, onSubmit }) {
  const [name, setName] = useState(files.length === 1 ? files[0].name.replace(/\.[^.]+$/, '') : 'archive')
  const [compress, setCompress] = useState(false)
  const total = files.reduce((a, f) => a + f.size, 0)
  return (
    <Modal title="Upload as zip" onClose={onClose}>
      <form onSubmit={(e) => { e.preventDefault(); onSubmit(files, name.trim() || 'archive', compress) }} className="space-y-3">
        <p className="text-sm text-ink-300">{files.length} file(s), {bytes(total)} total, will be zipped on the server and then sent to the channel.</p>
        <div><label className="label">Archive name</label><div className="flex items-center gap-1"><input className="input" value={name} onChange={(e) => setName(e.target.value)} /><span className="text-ink-400">.zip</span></div></div>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={compress} onChange={(e) => setCompress(e.target.checked)} /> Compress (slower; pointless for media and already-compressed files)</label>
        <div className="flex justify-end gap-2"><button type="button" className="btn-ghost" onClick={onClose}>Cancel</button><button className="btn-primary">Start</button></div>
      </form>
    </Modal>
  )
}
