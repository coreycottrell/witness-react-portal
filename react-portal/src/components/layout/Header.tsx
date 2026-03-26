import { useEffect, useState, useCallback } from 'react'
import { useIdentityStore } from '../../stores/identityStore'
import { StatusBadge } from '../common/StatusBadge'
import { apiGet } from '../../api/client'
import { Link } from 'react-router-dom'
import './Header.css'

interface ContextSnapshot {
  pct: number
  total_tokens: number
  max_tokens: number
}

function ctxColor(pct: number): string {
  if (pct < 50) return 'var(--status-success)'
  if (pct < 75) return 'var(--status-warning)'
  return 'var(--status-error)'
}

/** Mini SVG ring for the header */
function CtxRing({ pct }: { pct: number }) {
  const r = 12
  const circ = 2 * Math.PI * r
  const dash = (pct / 100) * circ

  return (
    <svg className="header-ctx-ring" width="32" height="32" viewBox="0 0 32 32">
      <circle cx="16" cy="16" r={r} fill="none" stroke="var(--bg-primary)" strokeWidth="3" />
      <circle
        cx="16" cy="16" r={r}
        fill="none"
        stroke={ctxColor(pct)}
        strokeWidth="3"
        strokeLinecap="round"
        strokeDasharray={`${dash} ${circ}`}
        transform="rotate(-90 16 16)"
        style={{ transition: 'stroke-dasharray 0.6s ease, stroke 0.3s ease' }}
      />
      <text
        x="16" y="16"
        textAnchor="middle"
        dominantBaseline="central"
        className="header-ctx-ring-text"
        style={{ fill: ctxColor(pct) }}
      >
        {Math.round(pct)}
      </text>
    </svg>
  )
}

export function Header() {
  const { civName, status } = useIdentityStore()
  const [ctx, setCtx] = useState<ContextSnapshot | null>(null)

  const fetchCtx = useCallback(async () => {
    try {
      const data = await apiGet<ContextSnapshot>('/api/context')
      setCtx(data)
    } catch {
      // silent
    }
  }, [])

  useEffect(() => {
    fetchCtx()
    const interval = setInterval(fetchCtx, 30_000)
    return () => clearInterval(interval)
  }, [fetchCtx])

  const claudeStatus = status?.claude_running ? 'online' : 'offline'

  return (
    <header className="header">
      <div className="header-left">
        <a href="https://ai-civ.com" target="_blank" rel="noopener noreferrer" className="header-brand-link">
          <h1 className="header-title">{civName || 'AiCIV'}</h1>
        </a>
        <span className="header-subtitle">Portal</span>
      </div>
      <div className="header-right">
        {ctx != null && (
          <Link to="/context" className="header-ctx-link" title="Context window — click for details">
            <CtxRing pct={ctx.pct} />
          </Link>
        )}
        <StatusBadge
          status={claudeStatus}
          label={claudeStatus === 'online' ? 'Active' : 'Offline'}
        />
      </div>
    </header>
  )
}
