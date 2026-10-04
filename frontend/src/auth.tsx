import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { getMe, logout, type Me } from './api'
import { AuthContext, useAuth } from './authContext'

const SIGNED_OUT: Me = { user: null, shop: null, dev_login: false }

// Loads "who is signed in?" once, and lets any page ask for it or refresh it.
export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null)

  const refresh = useCallback(async () => {
    try {
      setMe(await getMe())
    } catch {
      setMe(SIGNED_OUT) // can't reach the server: treat as signed out
    }
  }, [])

  const signOut = useCallback(async () => {
    try {
      await logout()
    } finally {
      await refresh()
    }
  }, [refresh])

  useEffect(() => {
    let live = true
    getMe()
      .then((m) => live && setMe(m))
      .catch(() => live && setMe(SIGNED_OUT))
    return () => {
      live = false
    }
  }, [])

  return <AuthContext.Provider value={{ me, refresh, signOut }}>{children}</AuthContext.Provider>
}

// Wrap a page that needs a signed-in user. Visitors are sent to sign in, then back here.
export function RequireAuth({ children }: { children: ReactNode }) {
  const { me } = useAuth()
  const location = useLocation()
  if (me === null) return <main className="narrow"><p className="muted">Loading…</p></main>
  if (!me.user) return <Navigate to={`/signin?next=${encodeURIComponent(location.pathname + location.search)}`} replace />
  return <>{children}</>
}
