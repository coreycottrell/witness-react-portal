import './witness.css'
/**
 * FleetPanel — Witness-only fleet management view
 *
 * Shows all running AiCIV containers, their health, and quick actions.
 * Data sourced from /api/witness/fleet (witness_extensions.py).
 */
import { useEffect, useState } from 'react'

interface FleetContainer {
  name: string
  status: string
  health: string
  port: number
  civ_name?: string
  human_name?: string
}

export function FleetPanel() {
  const [containers, setContainers] = useState<FleetContainer[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetch('/api/witness/fleet')
      .then(r => r.json())
      .then(data => {
        setContainers(data.containers ?? [])
        setLoading(false)
      })
      .catch(err => {
        setError(String(err))
        setLoading(false)
      })
  }, [])

  if (loading) return <div className="witness-panel">Loading fleet...</div>
  if (error) return <div className="witness-panel witness-panel--error">Fleet error: {error}</div>

  return (
    <div className="witness-panel">
      <h2 className="witness-panel__title">Fleet — {containers.length} containers</h2>
      <table className="witness-table">
        <thead>
          <tr>
            <th>Container</th>
            <th>CIV</th>
            <th>Human</th>
            <th>Status</th>
            <th>Health</th>
            <th>Port</th>
          </tr>
        </thead>
        <tbody>
          {containers.map(c => (
            <tr key={c.name}>
              <td><code>{c.name}</code></td>
              <td>{c.civ_name ?? '—'}</td>
              <td>{c.human_name ?? '—'}</td>
              <td className={`witness-status witness-status--${c.status}`}>{c.status}</td>
              <td className={`witness-health witness-health--${c.health}`}>{c.health}</td>
              <td>{c.port}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
