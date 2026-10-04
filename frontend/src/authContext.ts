import { createContext, useContext } from 'react'
import type { Me } from './api'

export interface AuthState {
  me: Me | null // null until the first answer arrives
  refresh: () => Promise<void>
  signOut: () => Promise<void>
}

export const AuthContext = createContext<AuthState | null>(null)

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>')
  return ctx
}

export const signInUrl = (next: string) => `/api/auth/paypal/login?next=${encodeURIComponent(next)}`
