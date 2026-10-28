import { useEffect, useState } from 'react'
import { fetchClaudeUsage, type ClaudeUsage, type UsageWindow } from '../../api/usage'
import { USAGE_POLL_MS, usageLevel, usagePct, type UsageLevel } from '../../utils/usage'
import './ClaudeUsageMeter.css'

function resetText(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return 'reset unknown'
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return 'reset unknown'
  const mins = Math.max(0, Math.round((t - now) / 60000))
  const h = Math.floor(mins / 60)
  const m = mins % 60
  const left = h >= 24 ? `${Math.floor(h / 24)}d ${h % 24}h` : h > 0 ? `${h}h ${m}m` : `${m}m`
  return `resets in ${left} (${new Date(t).toLocaleString()})`
}

interface BarSpec {
  key: string
  label: string
  win: UsageWindow | undefined
}

export function ClaudeUsageMeter() {
  const [usage, setUsage] = useState<ClaudeUsage | null>(null)

  useEffect(() => {
    let cancelled = false
    const poll = async () => {
      let next: ClaudeUsage
      try {
        next = await fetchClaudeUsage()
      } catch {
        next = { available: false, reason: 'network' }
      }
      if (!cancelled) setUsage(next)
    }
    void poll()
    const id = setInterval(poll, USAGE_POLL_MS)
    const onVisible = () => {
      if (document.visibilityState === 'visible') void poll()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      cancelled = true
      clearInterval(id)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [])

  if (usage === null) {
    return <span className="claude-usage claude-usage-na" data-testid="claude-usage">usage&hellip;</span>
  }

  if (!usage.available) {
    return (
      <span
        className="claude-usage claude-usage-na"
        data-testid="claude-usage"
        title={`Claude usage unavailable: ${usage.reason || 'unknown'}`}
      >
        usage n/a
      </span>
    )
  }

  const bars = ([
    { key: '5h', label: '5h', win: usage.five_hour },
    { key: 'week', label: 'Week', win: usage.seven_day },
  ] as BarSpec[]).map(({ key, label, win }) => {
    const inactive = win?.active === false
    const pct = inactive ? 0 : usagePct(win)
    const level: UsageLevel = pct === null ? 'ok' : usageLevel(pct)
    const shown = pct === null ? '?' : `${pct}%`
    const tip = inactive ? `${label}: no active window` : `${label}: ${shown}, ${resetText(win?.resets_at)}`
    return { key, label, pct, level, shown, tip }
  })
  const title = [...bars.map(b => b.tip), ...(usage.plan ? [`Plan: ${usage.plan}`] : [])].join('\n')

  return (
    <span className="claude-usage" data-testid="claude-usage" role="group" aria-label="Claude plan usage" title={title}>
      {bars.map(b => (
        <span key={b.key} className={`claude-usage-bar claude-usage-${b.level}`} data-level={b.level} data-testid={`claude-usage-${b.key}`}>
          <span className="claude-usage-text">{b.label} {b.shown}</span>
          <span className="claude-usage-track">
            <span className="claude-usage-fill" style={{ width: `${b.pct ?? 0}%` }} />
          </span>
        </span>
      ))}
    </span>
  )
}
