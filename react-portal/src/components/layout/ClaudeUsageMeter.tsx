import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchClaudeUsage, type ClaudeAccount, type ClaudeUsage, type UsageWindow } from '../../api/usage'
import {
  USAGE_POLL_MS,
  USAGE_RETRY_MS,
  detectChange,
  planLabel,
  readSeen,
  usageLevel,
  usagePct,
  writeSeen,
  type SeenAccount,
  type UsageLevel,
} from '../../utils/usage'
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

function clock(iso: string | null | undefined): string {
  if (!iso) return 'unknown'
  const t = new Date(iso)
  return Number.isNaN(t.getTime()) ? 'unknown' : t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

interface BarSpec {
  key: string
  label: string
  win: UsageWindow | undefined
}

type OkAccount = Extract<ClaudeAccount, { available: true }>

function accountName(a: OkAccount): string {
  return a.email || a.name || 'signed in'
}

export function ClaudeUsageMeter() {
  const [usage, setUsage] = useState<ClaudeUsage | null>(null)
  const [seen, setSeen] = useState<SeenAccount | null>(() => readSeen())
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLSpanElement | null>(null)
  const seenRef = useRef<SeenAccount | null>(seen)

  // First account ever seen on this device becomes the baseline; later switches are flagged against it
  // (the baseline is NOT moved by a switch: only an acknowledgement moves it).
  const noteAccount = useCallback((account: ClaudeAccount | undefined) => {
    if (!account || !account.available) return
    const prev = seenRef.current
    if (!prev) {
      const base = { key: account.key, email: account.email, ackAt: null }
      seenRef.current = base
      writeSeen(base)
      setSeen(base)
    } else if (prev.key === account.key && prev.email !== account.email) {
      const next = { ...prev, email: account.email }
      seenRef.current = next
      writeSeen(next)
      setSeen(next)
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const poll = async () => {
      let next: ClaudeUsage
      try {
        next = await fetchClaudeUsage()
      } catch {
        next = { available: false, reason: 'network' }
      }
      if (cancelled) return
      noteAccount(next.account)
      setUsage(next)
      clearTimeout(timer)
      timer = setTimeout(() => { void poll() }, next.available && !next.stale ? USAGE_POLL_MS : USAGE_RETRY_MS)
    }
    void poll()
    const onVisible = () => {
      if (document.visibilityState === 'visible') void poll()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      cancelled = true
      clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [noteAccount])

  // Close the details on an outside tap or Escape.
  useEffect(() => {
    if (!open) return
    const onDown = (e: Event) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('pointerdown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  if (usage === null) {
    return <span className="claude-usage claude-usage-na" data-testid="claude-usage">usage&hellip;</span>
  }

  const account = usage.account && usage.account.available ? usage.account : null
  const accountReason = !usage.account ? 'not reported by the server' : usage.account.available ? '' : usage.account.reason
  const change = account ? detectChange(account, seen) : null
  const plan = account ? planLabel(account.plan, account.tier) : null

  const ack = () => {
    if (!account) return
    const next = { key: account.key, email: account.email, ackAt: new Date().toISOString() }
    seenRef.current = next
    writeSeen(next)
    setSeen(next)
  }

  const stale = usage.available ? usage.stale : undefined
  const bars = usage.available
    ? (([
        { key: '5h', label: '5h', win: usage.five_hour },
        { key: 'week', label: 'Week', win: usage.seven_day },
      ] as BarSpec[]).map(({ key, label, win }) => {
        const inactive = win?.active === false
        const pct = inactive ? 0 : usagePct(win)
        const level: UsageLevel = pct === null ? 'ok' : usageLevel(pct)
        const shown = pct === null ? '?' : `${stale ? '~' : ''}${pct}%`
        const tip = inactive ? `${label}: no active window` : `${label}: ${shown}, ${resetText(win?.resets_at)}`
        return { key, label, pct, level, shown, tip }
      }))
    : []

  const accountTip = account
    ? `Claude account: ${accountName(account)}${plan ? ` (${plan})` : ''}`
    : `Claude account unknown (${accountReason || 'unknown'})`
  const changeTip = change
    ? `Account changed${change.previousEmail ? ` (was ${change.previousEmail})` : ''}`
    : ''
  const staleTip = stale ? `Last reading ${clock(stale.since)} (usage API ${stale.reason}); trying again` : ''
  const usageTip = usage.available
    ? [staleTip, ...bars.map(b => b.tip)].filter(Boolean).join('\n')
    : `Claude usage unavailable: ${usage.reason || 'unknown'}`
  const title = [accountTip, changeTip, usageTip, 'Tap for details'].filter(Boolean).join('\n')

  const accountState = !account ? 'unknown' : change ? 'changed' : 'ok'

  return (
    <span className="claude-usage-wrap" ref={wrapRef}>
      <button
        type="button"
        className={`claude-usage${usage.available ? '' : ' claude-usage-na'}`}
        data-testid="claude-usage"
        aria-label={`Claude account and plan usage. ${accountTip}. ${changeTip}. Tap for details.`}
        aria-expanded={open}
        title={title}
        onClick={() => setOpen(o => !o)}
      >
        <span className="claude-usage-account" data-testid="claude-usage-account" data-state={accountState}>
          <span className="claude-usage-email" data-testid="claude-usage-email">
            {account ? accountName(account) : 'account unknown'}
          </span>
          {change ? (
            <span className="claude-usage-plan claude-usage-changed" data-testid="claude-usage-changed">account changed</span>
          ) : plan ? (
            <span className="claude-usage-plan" data-testid="claude-usage-plan">{plan}</span>
          ) : null}
        </span>
        {usage.available ? (
          <span className={`claude-usage-bars${stale ? ' claude-usage-bars-stale' : ''}`} data-stale={stale ? 'true' : undefined}>
            {bars.map(b => (
              <span key={b.key} className={`claude-usage-bar claude-usage-${b.level}`} data-level={b.level} data-testid={`claude-usage-${b.key}`}>
                <span className="claude-usage-text">{b.label} {b.shown}</span>
                <span className="claude-usage-track">
                  <span className="claude-usage-fill" style={{ width: `${b.pct ?? 0}%` }} />
                </span>
              </span>
            ))}
          </span>
        ) : (
          <span className="claude-usage-bars claude-usage-bars-na" data-testid="claude-usage-na">usage n/a</span>
        )}
      </button>
      {open && (
        <div className="claude-usage-pop" role="dialog" aria-label="Claude account and usage" data-testid="claude-usage-popover">
          <div className="claude-usage-pop-row">
            <span className="claude-usage-pop-k">Signed in as</span>
            <span className="claude-usage-pop-v" data-testid="claude-usage-pop-account">
              {account ? accountName(account) : `unknown (${accountReason || 'unknown'})`}
            </span>
          </div>
          {account?.name && account.email && (
            <div className="claude-usage-pop-row">
              <span className="claude-usage-pop-k">Name</span>
              <span className="claude-usage-pop-v">{account.name}</span>
            </div>
          )}
          <div className="claude-usage-pop-row">
            <span className="claude-usage-pop-k">Plan</span>
            <span className="claude-usage-pop-v">{plan || 'unknown'}</span>
          </div>
          {change && (
            <div className="claude-usage-pop-change" role="status" data-testid="claude-usage-pop-change">
              The signed-in account changed{change.previousEmail ? <> (was {change.previousEmail})</> : null}.
              <button type="button" className="claude-usage-ack" data-testid="claude-usage-ack" onClick={ack}>Got it</button>
            </div>
          )}
          {usage.available ? (
            <>
              <div className="claude-usage-pop-row"><span className="claude-usage-pop-k">5-hour</span><span className="claude-usage-pop-v">{bars[0].tip.replace(/^5h: /, '')}</span></div>
              <div className="claude-usage-pop-row"><span className="claude-usage-pop-k">Week</span><span className="claude-usage-pop-v">{bars[1].tip.replace(/^Week: /, '')}</span></div>
              <div className="claude-usage-pop-foot">
                {stale
                  ? <>Showing the last reading from {clock(stale.since)} (usage API {stale.reason}). Trying again every minute.</>
                  : <>Checked {clock(usage.fetched_at)}</>}
              </div>
            </>
          ) : (
            <div className="claude-usage-pop-foot">Usage unavailable ({usage.reason || 'unknown'}). Trying again every minute.</div>
          )}
        </div>
      )}
    </span>
  )
}
