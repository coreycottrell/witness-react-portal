import { useEffect, useState, useRef } from 'react'
import { apiGet, apiPost } from '../../api/client'
import { fireFirstBoot } from '../../api/evolution'
import { RECONNECT_EVENT } from './ReconnectClaudeButton'
import './ClaudeAuthFlow.css'

interface AuthStatus {
  authenticated: boolean
  account: string | null
  expires_at: number | null
  needs_human_auth?: boolean
}

type FlowStep = 'idle' | 'starting' | 'polling_url' | 'waiting_code' | 'submitting' | 'firing_evolution' | 'done'

export function ClaudeAuthFlow() {
  const [status, setStatus] = useState<AuthStatus | null>(null)
  const [step, setStep] = useState<FlowStep>('idle')
  const [oauthUrl, setOauthUrl] = useState<string | null>(null)
  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  // forceOpen: user pressed "Reconnect Claude" — open the flow even when
  // /api/auth/status reports authenticated:true (the bug this fixes).
  const [forceOpen, setForceOpen] = useState(false)
  // reconnectMode: true when opened via the button on an already-live CIV, so
  // we DO NOT re-fire first-boot evolution after a successful re-login.
  const [reconnectMode, setReconnectMode] = useState(false)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetchStatus = async () => {
    try {
      const s = await apiGet<AuthStatus>('/api/auth/status')
      setStatus(s)
      return s
    } catch {
      return null
    }
  }

  useEffect(() => {
    fetchStatus()
    // On-demand re-auth: the "Reconnect Claude" button dispatches this event.
    const onReconnect = () => {
      if (pollRef.current) clearInterval(pollRef.current)
      setError(null)
      setOauthUrl(null)
      setCode('')
      setStep('idle')
      setReconnectMode(true)
      setForceOpen(true)
      fetchStatus()
    }
    window.addEventListener(RECONNECT_EVENT, onReconnect)
    return () => {
      window.removeEventListener(RECONNECT_EVENT, onReconnect)
      if (pollRef.current) clearInterval(pollRef.current)
    }
  }, [])

  const closeFlow = () => {
    if (pollRef.current) clearInterval(pollRef.current)
    setForceOpen(false)
    setReconnectMode(false)
    setStep('idle')
    setError(null)
    setOauthUrl(null)
    setCode('')
  }

  const startAuth = async () => {
    setError(null)
    setStep('starting')
    try {
      await apiPost('/api/auth/start')
      setStep('polling_url')
      pollRef.current = setInterval(async () => {
        try {
          const res = await apiGet<{ url?: string; ready: boolean }>('/api/auth/url')
          if (res.ready && res.url) {
            clearInterval(pollRef.current!)
            setOauthUrl(res.url)
            setStep('waiting_code')
          }
        } catch {
          // keep polling
        }
      }, 2000)
    } catch {
      setError('Failed to start auth flow. Is Claude running in tmux?')
      setStep('idle')
    }
  }

  const submitCode = async () => {
    if (!code.trim()) return
    setStep('submitting')
    setError(null)
    try {
      await apiPost('/api/auth/code', { code: code.trim() })
      // Poll status until authenticated, then fire evolution
      pollRef.current = setInterval(async () => {
        const s = await fetchStatus()
        if (s?.authenticated) {
          clearInterval(pollRef.current!)
          // Reconnect (already-live CIV) must NOT re-fire birth evolution.
          if (reconnectMode) {
            setStep('done')
            setTimeout(closeFlow, 2500)
            return
          }
          setStep('firing_evolution')
          try {
            await fireFirstBoot()
          } catch {
            setError('Auth succeeded but failed to start evolution. Refresh and try again.')
          }
          setStep('done')
        }
      }, 2000)
    } catch {
      setError('Failed to submit code. Try again.')
      setStep('waiting_code')
    }
  }

  // Render when Claude is unauthenticated OR the user explicitly asked to
  // reconnect (forceOpen). Without forceOpen this returns null on a live CIV —
  // which is exactly why the re-auth modal never surfaced (ticket 2153).
  if (!forceOpen && (!status || status.authenticated)) return null

  return (
    <div className="claude-auth-overlay">
      <div className="claude-auth-card" style={{ position: 'relative' }}>
        {forceOpen && step !== 'done' && (
          <button
            type="button"
            className="claude-auth-close"
            onClick={closeFlow}
            aria-label="Cancel reconnect"
            title="Cancel"
            style={{
              position: 'absolute', top: '12px', right: '12px',
              background: 'transparent', border: 'none',
              color: 'var(--text-secondary, #999)', fontSize: '20px',
              lineHeight: 1, cursor: 'pointer',
            }}
          >
            ×
          </button>
        )}
        <div className="claude-auth-header">
          <h2 className="claude-auth-title">
            {reconnectMode ? 'Reconnect Claude' : 'Claude Authentication Required'}
          </h2>
          <p className="claude-auth-subtitle">
            {reconnectMode
              ? 'Sign in again to refresh your Claude login. Your memory, identity, and files are untouched.'
              : 'Claude Code needs to authenticate with Anthropic before you can chat.'}
          </p>
        </div>

        {error && <p className="claude-auth-error">{error}</p>}

        {step === 'idle' && (
          <button className="claude-auth-btn" onClick={startAuth}>
            Start Auth
          </button>
        )}

        {step === 'starting' && (
          <p className="claude-auth-status">Starting auth flow in Claude tmux session...</p>
        )}

        {step === 'polling_url' && (
          <p className="claude-auth-status">Waiting for OAuth URL from Claude... (this takes ~5s)</p>
        )}

        {step === 'waiting_code' && oauthUrl && (
          <div className="claude-auth-code-step">
            <p className="claude-auth-instruction">
              1. Open this link and log in with your Anthropic account:
            </p>
            <a
              className="claude-auth-link"
              href={oauthUrl}
              target="_blank"
              rel="noopener noreferrer"
            >
              Open Anthropic Login
            </a>
            <p className="claude-auth-instruction">
              2. After logging in, paste the authorization code below:
            </p>
            <input
              className="claude-auth-input"
              type="text"
              placeholder="Paste authorization code..."
              value={code}
              onChange={e => setCode(e.target.value)}
              autoFocus
            />
            <button
              className="claude-auth-btn"
              onClick={submitCode}
              disabled={!code.trim()}
            >
              Submit Code
            </button>
          </div>
        )}

        {step === 'submitting' && (
          <p className="claude-auth-status">Submitting code... verifying with Anthropic...</p>
        )}

        {step === 'firing_evolution' && (
          <p className="claude-auth-status">Authenticated! Starting evolution...</p>
        )}

        {step === 'done' && (
          <p className="claude-auth-status claude-auth-success">
            Authenticated successfully! Your AiCIV is waking up...
          </p>
        )}
      </div>
    </div>
  )
}
