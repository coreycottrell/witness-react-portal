import { create } from 'zustand'
import { AUTH_TOKEN_KEY } from '../utils/constants'
import { fetchStatus } from '../api/identity'

interface AuthState {
  token: string | null
  authenticated: boolean
  loading: boolean
  error: string | null
  login: (token: string) => Promise<boolean>
  logout: () => void
  checkAuth: () => Promise<boolean>
}

export const useAuthStore = create<AuthState>((set, get) => ({
  token: localStorage.getItem(AUTH_TOKEN_KEY),
  authenticated: false,
  loading: true,
  error: null,

  login: async (token: string) => {
    localStorage.setItem(AUTH_TOKEN_KEY, token)
    set({ token, loading: true, error: null })
    try {
      await fetchStatus()
      set({ authenticated: true, loading: false })
      return true
    } catch {
      localStorage.removeItem(AUTH_TOKEN_KEY)
      set({ token: null, authenticated: false, loading: false, error: 'Invalid token' })
      return false
    }
  },

  logout: () => {
    localStorage.removeItem(AUTH_TOKEN_KEY)
    set({ token: null, authenticated: false, error: null })
  },

  checkAuth: async () => {
    // Auto-extract bearer token from magic link URL (?token=XXX)
    const params = new URLSearchParams(window.location.search)
    const urlToken = params.get('token')
    if (urlToken) {
      localStorage.setItem(AUTH_TOKEN_KEY, urlToken)
      set({ token: urlToken })
      // Clean token from URL without reloading
      const cleanUrl = window.location.pathname + window.location.hash
      window.history.replaceState({}, '', cleanUrl)
    }

    const { token } = get()
    if (!token) {
      set({ loading: false, authenticated: false })
      return false
    }
    try {
      await fetchStatus()
      set({ authenticated: true, loading: false })
      return true
    } catch {
      localStorage.removeItem(AUTH_TOKEN_KEY)
      set({ token: null, authenticated: false, loading: false })
      return false
    }
  },
}))
