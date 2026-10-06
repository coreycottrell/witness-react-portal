/**
 * PointsSummary — collapsed bar with running totals + expandable detail.
 */
import { useEffect, useState, useCallback } from 'react'
import { apiGet } from '../../api/client'

interface PointsSummaryData {
  primary: number
  corey: number
  team_leads: number
}

interface PointsHistoryEntry {
  timestamp: string
  recipient: string
  amount: number
  note: string
}

function formatPoints(n: number): string {
  return n >= 0 ? `+${n}` : `${n}`
}

export function PointsSummary() {
  const [summary, setSummary] = useState<PointsSummaryData | null>(null)
  const [history, setHistory] = useState<PointsHistoryEntry[]>([])
  const [expanded, setExpanded] = useState(false)
  const [historyLoaded, setHistoryLoaded] = useState(false)

  const fetchSummary = useCallback(() => {
    apiGet<PointsSummaryData>('/api/points/summary')
      .then(setSummary)
      .catch(() => {})
  }, [])

  useEffect(() => { fetchSummary() }, [fetchSummary])

  const toggleExpand = () => {
    if (!expanded && !historyLoaded) {
      apiGet<PointsHistoryEntry[]>('/api/points/history')
        .then(data => { setHistory(data); setHistoryLoaded(true) })
        .catch(() => {})
    }
    setExpanded(!expanded)
  }

  if (!summary) return null

  return (
    <div className="points-summary">
      <div className="points-summary__bar" onClick={toggleExpand}>
        <span className="points-summary__item">
          Primary: <span className={summary.primary >= 0 ? 'points-positive' : 'points-negative'}>
            {formatPoints(summary.primary)}
          </span>
        </span>
        <span className="points-summary__divider">|</span>
        <span className="points-summary__item">
          Corey: <span className={summary.corey >= 0 ? 'points-positive' : 'points-negative'}>
            {formatPoints(summary.corey)}
          </span>
        </span>
        <span className="points-summary__divider">|</span>
        <span className="points-summary__item">
          Team Leads: <span className={summary.team_leads >= 0 ? 'points-positive' : 'points-negative'}>
            {formatPoints(summary.team_leads)}
          </span>
        </span>
        <button className="points-summary__toggle">
          {expanded ? 'Collapse' : 'Expand'}
        </button>
      </div>

      {expanded && history.length > 0 && (
        <div className="points-summary__detail">
          <table className="points-summary__table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Recipient</th>
                <th>Amount</th>
                <th>Note</th>
              </tr>
            </thead>
            <tbody>
              {history.slice(0, 50).map((entry, i) => (
                <tr key={i}>
                  <td className="points-summary__ts">
                    {entry.timestamp ? new Date(entry.timestamp).toLocaleString(undefined, {
                      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
                    }) : '—'}
                  </td>
                  <td>{entry.recipient}</td>
                  <td className={entry.amount >= 0 ? 'points-positive' : 'points-negative'}>
                    {formatPoints(entry.amount)}
                  </td>
                  <td className="points-summary__note">{entry.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
