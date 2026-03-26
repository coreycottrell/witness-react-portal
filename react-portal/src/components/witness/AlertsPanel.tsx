/**
 * AlertsPanel — Witness-only operational alerts
 *
 * Shows birth failures, support emails, unhealthy containers, pending actions.
 * Data sourced from /api/witness/alerts (witness_extensions.py).
 */
import { useEffect, useState } from 'react'

type AlertSeverity = 'critical' | 'warning' | 'info'

interface Alert {
  id: string
  severity: AlertSeverity
  title: string
  body: string
  timestamp: string
  acknowledged: boolean
}

export function AlertsPanel() {
  const [alerts, setAlerts] = useState<Alert[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const refresh = () => {
    setLoading(true)
    fetch('/api/witness/alerts')
      .then(r => r.json())
      .then(data => {
        setAlerts(data.alerts ?? [])
        setLoading(false)
      })
      .catch(err => {
        setError(String(err))
        setLoading(false)
      })
  }

  useEffect(() => { refresh() }, [])

  const acknowledge = async (id: string) => {
    await fetch(`/api/witness/alerts/${id}/ack`, { method: 'POST' })
    refresh()
  }

  if (loading) return <div className="witness-panel">Loading alerts...</div>
  if (error) return <div className="witness-panel witness-panel--error">Alerts error: {error}</div>

  const unacked = alerts.filter(a => !a.acknowledged)

  return (
    <div className="witness-panel">
      <h2 className="witness-panel__title">
        Alerts {unacked.length > 0 && <span className="witness-badge">{unacked.length}</span>}
      </h2>
      {alerts.length === 0 && <p className="witness-empty">No active alerts. All systems nominal.</p>}
      <div className="witness-alerts-list">
        {alerts.map(a => (
          <div key={a.id} className={`witness-alert witness-alert--${a.severity} ${a.acknowledged ? 'witness-alert--acked' : ''}`}>
            <div className="witness-alert__header">
              <span className={`witness-alert__severity`}>{a.severity.toUpperCase()}</span>
              <span className="witness-alert__title">{a.title}</span>
              <span className="witness-alert__time">{new Date(a.timestamp).toLocaleString()}</span>
            </div>
            <p className="witness-alert__body">{a.body}</p>
            {!a.acknowledged && (
              <button className="witness-btn witness-btn--sm" onClick={() => acknowledge(a.id)}>
                Acknowledge
              </button>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
