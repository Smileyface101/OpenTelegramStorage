import { NavLink, Outlet, Link } from 'react-router-dom'
import { FolderOpen, ArrowLeftRight, Settings as Cog, LogOut, Send, AlertTriangle } from 'lucide-react'
import { post } from '../lib/api'

const nav = [
  { to: '/files', label: 'Files', icon: FolderOpen },
  { to: '/transfers', label: 'Transfers', icon: ArrowLeftRight },
  { to: '/settings', label: 'Settings', icon: Cog },
]

export default function Layout({ user, onLogout, setup }) {
  const logout = async () => { try { await post('/api/auth/logout') } finally { onLogout() } }
  const tgOk = setup?.telegram_connected && setup?.channel_configured
  return (
    <div className="min-h-screen flex flex-col md:flex-row">
      <aside className="md:w-56 border-b md:border-b-0 md:border-r border-ink-800 bg-ink-900/60 p-4 flex md:flex-col gap-2 md:gap-1 items-center md:items-stretch">
        <Link to="/files" className="flex items-center gap-2 font-semibold text-ink-200 md:mb-4 mr-auto md:mr-0">
          <span className="w-8 h-8 rounded-lg bg-brand-500 grid place-items-center text-white"><Send size={16} /></span>
          <span className="hidden sm:inline">OpenTelegramHosting</span>
        </Link>
        {nav.map(({ to, label, icon: Icon }) => (
          <NavLink key={to} to={to} className={({ isActive }) =>
            `flex items-center gap-2 rounded-lg px-3 py-2 text-sm ${isActive ? 'bg-ink-800 text-white' : 'text-ink-300 hover:bg-ink-800/60'}`}>
            <Icon size={16} /><span className="hidden sm:inline">{label}</span>
          </NavLink>
        ))}
        <div className="md:mt-auto flex md:flex-col items-center md:items-stretch gap-2 text-xs text-ink-400">
          <div className="hidden md:flex items-center gap-2 px-3">
            <span className={`w-2 h-2 rounded-full ${tgOk ? 'bg-emerald-400' : 'bg-amber-400'}`} />
            {tgOk ? 'Telegram connected' : 'Telegram not ready'}
          </div>
          <div className="hidden md:block px-3 truncate">Signed in as <b className="text-ink-200">{user?.username}</b></div>
          <button onClick={logout} className="btn-ghost justify-center"><LogOut size={14} /><span className="hidden sm:inline">Sign out</span></button>
        </div>
      </aside>
      <main className="flex-1 p-4 md:p-8 max-w-6xl w-full mx-auto">
        {!tgOk && (
          <div className="mb-4 flex items-center gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-200">
            <AlertTriangle size={16} /> Telegram is not fully configured. Uploads will queue until it is.
            {user?.role === 'admin' && <Link to="/setup" className="underline ml-1">Finish setup</Link>}
          </div>
        )}
        <Outlet />
      </main>
    </div>
  )
}
