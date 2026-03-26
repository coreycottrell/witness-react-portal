import { useEffect, useState } from 'react'
import { apiGet } from '../../api/client'
import { formatUptime } from '../../utils/time'
import { LoadingSpinner } from '../common/LoadingSpinner'
import './StatusView.css'

interface StatusData {
  civ: string
  uptime: number
  tmux_session: string
  tmux_alive: boolean
  claude_running: boolean
  tg_bot_running: boolean
  ctx_pct: number | null
  version: string
  timestamp: number
}

interface BoopStatus {
  running: boolean
  pid?: number
}

interface AuthStatus {
  status: string
  expires_at?: number
}

export function StatusView() {
  const [status, setStatus] = useState<StatusData | null>(null)
  const [boop, setBoop] = useState<BoopStatus | null>(null)
  const [auth, setAuth] = useState<AuthStatus | null>(null)
  const [loading, setLoading] = useState(true)

  const fetchAll = async () => {
    try {
      const [s, b, a] = await Promise.allSettled([
        apiGet<StatusData>('/api/status'),
        apiGet<BoopStatus>('/api/boop-status'),
        apiGet<AuthStatus>('/api/claude-auth-status'),
      ])
      if (s.status === 'fulfilled') setStatus(s.value)
      if (b.status === 'fulfilled') setBoop(b.value)
      if (a.status === 'fulfilled') setAuth(a.value)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchAll()
    const interval = setInterval(fetchAll, 15_000)
    return () => clearInterval(interval)
  }, [])

  if (loading) {
    return <div className="status-loading"><LoadingSpinner size={32} /></div>
  }

  const indicator = (ok: boolean | undefined | null) =>
    ok ? '\u{1F7E2}' : '\u{1F534}'

  const ctxPct = status?.ctx_pct ?? 0

  return (
    <div className="status-view">
      <h2 className="status-title">Status Dashboard</h2>
      <div className="status-grid">
        <div className="status-card">
          <h3 className="status-card-title">CIV Identity</h3>
          <div className="status-card-body">
            <div className="status-row">
              <span className="status-label">Name</span>
              <span className="status-value">{status?.civ ?? '—'}</span>
            </div>
            <div className="status-row">
              <span className="status-label">Uptime</span>
              <span className="status-value">{status ? formatUptime(status.uptime) : '—'}</span>
            </div>
            <div className="status-row">
              <span className="status-label">Version</span>
              <span className="status-value">{status?.version ?? '—'}</span>
            </div>
          </div>
        </div>

        <div className="status-card">
          <h3 className="status-card-title">Process Health</h3>
          <div className="status-card-body">
            <div className="status-row">
              <span className="status-label">tmux</span>
              <span className="status-value">{indicator(status?.tmux_alive)} {status?.tmux_alive ? 'alive' : 'down'}</span>
            </div>
            <div className="status-row">
              <span className="status-label">Claude</span>
              <span className="status-value">{indicator(status?.claude_running)} {status?.claude_running ? 'running' : 'stopped'}</span>
            </div>
            <div className="status-row">
              <span className="status-label">Telegram</span>
              <span className="status-value">{indicator(status?.tg_bot_running)} {status?.tg_bot_running ? 'running' : 'stopped'}</span>
            </div>
          </div>
        </div>

        <div className="status-card">
          <h3 className="status-card-title">Context Window</h3>
          <div className="status-card-body">
            <div className="status-ctx-bar">
              <div className="status-ctx-fill" style={{ width: `${Math.min(ctxPct, 100)}%` }} />
            </div>
            <div className="status-row">
              <span className="status-label">Usage</span>
              <span className="status-value">{ctxPct > 0 ? `${ctxPct.toFixed(1)}%` : 'N/A'}</span>
            </div>
          </div>
        </div>

        <div className="status-card">
          <h3 className="status-card-title">Boop Daemon</h3>
          <div className="status-card-body">
            <div className="status-row">
              <span className="status-label">Status</span>
              <span className="status-value">{indicator(boop?.running)} {boop?.running ? 'running' : 'stopped'}</span>
            </div>
          </div>
        </div>

        <div className="status-card">
          <h3 className="status-card-title">Claude Auth</h3>
          <div className="status-card-body">
            <div className="status-row">
              <span className="status-label">Status</span>
              <span className="status-value">{auth?.status ?? '—'}</span>
            </div>
            {auth?.expires_at && (
              <div className="status-row">
                <span className="status-label">Expires</span>
                <span className="status-value">{new Date(auth.expires_at).toLocaleString()}</span>
              </div>
            )}
          </div>
        </div>

        <div className="status-card">
          <h3 className="status-card-title">Session</h3>
          <div className="status-card-body">
            <div className="status-row">
              <span className="status-label">tmux session</span>
              <span className="status-value status-mono">{status?.tmux_session ?? '—'}</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
