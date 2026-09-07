import { useCallback, useEffect, useState } from 'react'
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { get } from './lib/api'
import Layout from './components/Layout'
import Setup from './pages/Setup'
import Login from './pages/Login'
import Files from './pages/Files'
import Transfers from './pages/Transfers'
import Settings from './pages/Settings'

export default function App() {
  const [boot, setBoot] = useState({ loading: true, setup: null, user: null })
  const navigate = useNavigate()
  const location = useLocation()

  const refresh = useCallback(async () => {
    const setup = await get('/api/setup/status')
    let user = null
    if (!setup.needs_admin) {
      try { user = await get('/api/auth/me') } catch { user = null }
    }
    setBoot({ loading: false, setup, user })
    return { setup, user }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  if (boot.loading) return <Splash />
  const { setup, user } = boot

  if (setup.needs_admin && location.pathname !== '/setup') return <Navigate to="/setup" replace />
  if (!setup.needs_admin && !user && location.pathname !== '/login') return <Navigate to="/login" replace />

  const onAuth = async () => {
    const st = await refresh()
    const needsWizard = st.user?.role === 'admin' && !st.setup.channel_configured
    navigate(needsWizard ? '/setup' : '/files', { replace: true })
  }
  const onLogout = async () => { await refresh(); navigate('/login', { replace: true }) }

  return (
    <Routes>
      <Route path="/setup" element={<Setup setup={setup} user={user} onDone={async () => { await refresh(); navigate('/files') }} onAdminCreated={refresh} />} />
      <Route path="/login" element={user ? <Navigate to="/files" replace /> : <Login onAuth={onAuth} />} />
      <Route element={<Layout user={user} onLogout={onLogout} setup={setup} />}>
        <Route path="/files" element={<Files />} />
        <Route path="/transfers" element={<Transfers />} />
        <Route path="/settings" element={<Settings user={user} onChange={refresh} />} />
        <Route path="*" element={<Navigate to="/files" replace />} />
      </Route>
    </Routes>
  )
}

function Splash() {
  return (
    <div className="min-h-screen flex items-center justify-center text-ink-400 text-sm">Loading…</div>
  )
}
