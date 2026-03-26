import { useState, type FormEvent } from 'react'
import { useAuthStore } from '../../stores/authStore'
import './AuthModal.css'

export function AuthModal() {
  const [token, setToken] = useState('')
  const [loading, setLoading] = useState(false)
  const { login, error } = useAuthStore()

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    if (!token.trim()) return
    setLoading(true)
    await login(token.trim())
    setLoading(false)
  }

  return (
    <div className="auth-overlay">
      <div className="auth-card">
        <div className="auth-header">
          <h1 className="auth-title">AiCIV Portal</h1>
          <p className="auth-subtitle">Enter your access token to continue</p>
        </div>
        <form onSubmit={handleSubmit} className="auth-form">
          <input
            type="password"
            className="auth-input"
            placeholder="Bearer token..."
            value={token}
            onChange={e => setToken(e.target.value)}
            autoFocus
            disabled={loading}
          />
          {error && <p className="auth-error">{error}</p>}
          <button
            type="submit"
            className="auth-submit"
            disabled={loading || !token.trim()}
          >
            {loading ? 'Authenticating...' : 'Login'}
          </button>
        </form>
      </div>
    </div>
  )
}
