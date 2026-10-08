import './witness.css'
/**
 * FleetPanel — Witness-only fleet management view
 *
 * Shows all running AiCIV containers with SSH copy, portal links,
 * search/filter, auto-refresh, and responsive card layout on mobile.
 * Data sourced from /api/witness/fleet (witness_extensions.py).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

interface FleetContainer {
  name: string
  civ_name: string
  human: string
  status: string
  portal_url: string | null
  ssh_command: string | null
  ssh_port: number | null
  api_port: number | null
  host_ip: string | null
  tmux_session: string | null
}

const GREEN_STATUSES = new Set(['running', 'alive', 'active', 'operational'])

function statusColor(status: string): string {
  if (GREEN_STATUSES.has(status)) return 'running'
  if (status === 'stopped') return 'stopped'
  return 'other'
}

function CopySSHButton({ sshCommand }: { sshCommand: string | null }) {
  const [copied, setCopied] = useState(false)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  if (!sshCommand) return <span>—</span>

  const handleCopy = () => {
    navigator.clipboard.writeText(sshCommand).then(() => {
      setCopied(true)
      if (timerRef.current) clearTimeout(timerRef.current)
      timerRef.current = setTimeout(() => setCopied(false), 1500)
    })
  }

  return (
    <button
      className={`witness-btn witness-btn--sm witness-btn-copy${copied ? ' witness-btn-copy--copied' : ''}`}
      onClick={handleCopy}
      title={sshCommand}
    >
      {copied ? 'Copied!' : 'Copy SSH'}
    </button>
  )
}

function PortalLink({ url }: { url: string | null }) {
  if (!url) return <span>—</span>
  return (
    <a
      className="witness-btn witness-btn--sm witness-btn-portal"
      href={url}
      target="_blank"
      rel="noopener noreferrer"
    >
      Open
    </a>
  )
}

export function FleetPanel() {
  const [containers, setContainers] = useState<FleetContainer[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [search, setSearch] = useState('')

  const fetchFleet = useCallback(() => {
    fetch('/api/witness/fleet')
      .then(r => r.json())
      .then(data => {
        setContainers(data.containers ?? [])
        setLoading(false)
        setError(null)
      })
      .catch(err => {
        setError(String(err))
        setLoading(false)
      })
  }, [])

  // Initial fetch + auto-refresh every 60s
  useEffect(() => {
    fetchFleet()
    const interval = setInterval(fetchFleet, 60_000)
    return () => clearInterval(interval)
  }, [fetchFleet])

  const filtered = useMemo(() => {
    if (!search.trim()) return containers
    const q = search.toLowerCase()
    return containers.filter(
      c =>
        c.name.toLowerCase().includes(q) ||
        (c.civ_name && c.civ_name.toLowerCase().includes(q)) ||
        (c.human && c.human.toLowerCase().includes(q))
    )
  }, [containers, search])

  if (loading) return <div className="witness-panel">Loading fleet...</div>
  if (error) return <div className="witness-panel witness-panel--error">Fleet error: {error}</div>

  return (
    <div className="witness-panel">
      <h2 className="witness-panel__title">
        Fleet — {filtered.length} of {containers.length} containers
      </h2>

      <div className="witness-fleet-search">
        <input
          type="text"
          placeholder="Filter by container, CIV, or human name..."
          value={search}
          onChange={e => setSearch(e.target.value)}
        />
      </div>

      {/* Desktop table */}
      <table className="witness-table witness-fleet-table-desktop">
        <thead>
          <tr>
            <th>Container</th>
            <th>CIV</th>
            <th>Human</th>
            <th>Status</th>
            <th>SSH</th>
            <th>Portal</th>
          </tr>
        </thead>
        <tbody>
          {filtered.map(c => (
            <tr key={c.name}>
              <td><code>{c.name}</code></td>
              <td>{c.civ_name || '—'}</td>
              <td>{c.human || '—'}</td>
              <td>
                <span className={`witness-status witness-status--${statusColor(c.status)}`}>
                  {c.status}
                </span>
              </td>
              <td>
                <div className="witness-fleet-actions">
                  <CopySSHButton sshCommand={c.ssh_command} />
                </div>
              </td>
              <td>
                <div className="witness-fleet-actions">
                  <PortalLink url={c.portal_url} />
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {/* Mobile card layout */}
      <div className="witness-fleet-cards-mobile">
        {filtered.map(c => (
          <div key={c.name} className="witness-fleet-card">
            <div className="witness-fleet-card__header">
              <code>{c.name}</code>
              <span className={`witness-status witness-status--${statusColor(c.status)}`}>
                {c.status}
              </span>
            </div>
            <div className="witness-fleet-card__details">
              <span><strong>CIV:</strong> {c.civ_name || '—'}</span>
              <span><strong>Human:</strong> {c.human || '—'}</span>
            </div>
            <div className="witness-fleet-actions">
              <CopySSHButton sshCommand={c.ssh_command} />
              <PortalLink url={c.portal_url} />
            </div>
          </div>
        ))}
      </div>

      {filtered.length === 0 && (
        <div className="witness-empty">No containers match your filter.</div>
      )}
    </div>
  )
}
