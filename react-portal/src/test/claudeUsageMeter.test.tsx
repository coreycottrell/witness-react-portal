import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, fireEvent } from '@testing-library/react'
import { ClaudeUsageMeter } from '../components/layout/ClaudeUsageMeter'
import headerSrc from '../components/layout/Header.tsx?raw'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import {
  usageLevel, usagePct, planLabel, detectChange, readSeen, ACCOUNT_SEEN_KEY, USAGE_POLL_MS, USAGE_RETRY_MS,
} from '../utils/usage'
import * as usageApi from '../api/usage'

type Acct = Extract<usageApi.ClaudeAccount, { available: true }>
const acct = (over: Partial<Acct> = {}): Acct => ({
  available: true, email: 'russell@example.com', name: 'Russell', plan: 'max', tier: '20x', key: 'aaaaaaaaaaaa', changed: null, ...over,
})

const ok = (
  five: number | null,
  week: number | null,
  extra: Partial<{ fiveActive: boolean; plan: string | null; account: usageApi.ClaudeAccount | undefined }> = {},
): usageApi.ClaudeUsage => ({
  available: true as const,
  five_hour: { utilization: five, resets_at: '2026-10-28T17:00:00+00:00', active: extra.fiveActive ?? true },
  seven_day: { utilization: week, resets_at: '2026-11-01T15:00:00+00:00', active: true },
  plan: extra.plan === undefined ? 'max' : extra.plan,
  fetched_at: '2026-10-28T16:00:00+00:00',
  account: 'account' in extra ? extra.account : acct(),
})

async function flush() {
  await act(async () => { await Promise.resolve(); await Promise.resolve() })
}

describe('usageLevel / usagePct', () => {
  it('thresholds: ok < 70 <= warn < 90 <= crit', () => {
    expect(usageLevel(0)).toBe('ok')
    expect(usageLevel(69)).toBe('ok')
    expect(usageLevel(70)).toBe('warn')
    expect(usageLevel(89)).toBe('warn')
    expect(usageLevel(90)).toBe('crit')
    expect(usageLevel(100)).toBe('crit')
  })
  it('clamps and rounds; null for non-numbers', () => {
    expect(usagePct({ utilization: 48.4, resets_at: null, active: true })).toBe(48)
    expect(usagePct({ utilization: 140, resets_at: null, active: true })).toBe(100)
    expect(usagePct({ utilization: -5, resets_at: null, active: true })).toBe(0)
    expect(usagePct({ utilization: null, resets_at: null, active: true })).toBeNull()
    expect(usagePct(undefined)).toBeNull()
  })
})

describe('planLabel', () => {
  it('formats plan + tier, and never guesses', () => {
    expect(planLabel('max', '20x')).toBe('Max 20x')
    expect(planLabel('pro', null)).toBe('Pro')
    expect(planLabel(null, '20x')).toBeNull()
    expect(planLabel(undefined, undefined)).toBeNull()
  })
})

describe('detectChange', () => {
  it('no baseline and no server flag -> no change', () => {
    expect(detectChange(acct(), null)).toBeNull()
  })
  it('device baseline with a different key -> flagged with the previous email', () => {
    const c = detectChange(acct({ key: 'bbbbbbbbbbbb' }), { key: 'aaaaaaaaaaaa', email: 'old@example.com', ackAt: null })
    expect(c).toEqual({ previousEmail: 'old@example.com', source: 'device' })
  })
  it('same key, no server flag -> no change', () => {
    expect(detectChange(acct(), { key: 'aaaaaaaaaaaa', email: 'russell@example.com', ackAt: null })).toBeNull()
  })
  it('server flag is shown until this device acknowledged it', () => {
    const a = acct({ changed: { at: '2026-10-28T16:00:00+00:00', previous_email: 'prev@example.com' } })
    expect(detectChange(a, { key: 'aaaaaaaaaaaa', email: null, ackAt: null })?.source).toBe('server')
    expect(detectChange(a, { key: 'aaaaaaaaaaaa', email: null, ackAt: '2026-10-28T15:00:00+00:00' })?.source).toBe('server')
    expect(detectChange(a, { key: 'aaaaaaaaaaaa', email: null, ackAt: '2026-10-28T16:30:00+00:00' })).toBeNull()
  })
})

describe('ClaudeUsageMeter', () => {
  beforeEach(() => { vi.useFakeTimers(); localStorage.clear() })
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

  it('renders both windows with level classes and a reset-time tooltip', async () => {
    vi.spyOn(usageApi, 'fetchClaudeUsage').mockResolvedValue(ok(92, 71))
    render(<ClaudeUsageMeter />)
    await flush()
    expect(screen.getByTestId('claude-usage-5h')).toHaveAttribute('data-level', 'crit')
    expect(screen.getByTestId('claude-usage-5h').textContent).toContain('5h 92%')
    expect(screen.getByTestId('claude-usage-week')).toHaveAttribute('data-level', 'warn')
    const title = screen.getByTestId('claude-usage').getAttribute('title') || ''
    expect(title).toContain('5h: 92%')
    expect(title).toContain('Week: 71%')
  })

  it('shows the signed-in account (email + plan) with a tooltip', async () => {
    vi.spyOn(usageApi, 'fetchClaudeUsage').mockResolvedValue(ok(10, 20))
    render(<ClaudeUsageMeter />)
    await flush()
    expect(screen.getByTestId('claude-usage-email').textContent).toBe('russell@example.com')
    expect(screen.getByTestId('claude-usage-plan').textContent).toBe('Max 20x')
    expect(screen.getByTestId('claude-usage-account')).toHaveAttribute('data-state', 'ok')
    const title = screen.getByTestId('claude-usage').getAttribute('title') || ''
    expect(title).toContain('Claude account: russell@example.com (Max 20x)')
  })

  it('an inactive window shows 0% and "no active window"', async () => {
    vi.spyOn(usageApi, 'fetchClaudeUsage').mockResolvedValue(ok(0, 10, { fiveActive: false }))
    render(<ClaudeUsageMeter />)
    await flush()
    expect(screen.getByTestId('claude-usage-5h')).toHaveAttribute('data-level', 'ok')
    expect(screen.getByTestId('claude-usage').getAttribute('title')).toContain('5h: no active window')
  })

  it('an unreadable account is an explicit unknown marker, never a stale name', async () => {
    const spy = vi.spyOn(usageApi, 'fetchClaudeUsage').mockResolvedValue(ok(10, 20))
    render(<ClaudeUsageMeter />)
    await flush()
    expect(screen.getByTestId('claude-usage-email').textContent).toBe('russell@example.com')
    spy.mockResolvedValue(ok(10, 20, { account: { available: false, reason: 'expired' } }))
    await act(async () => { vi.advanceTimersByTime(USAGE_POLL_MS) })
    await flush()
    expect(screen.getByTestId('claude-usage-email').textContent).toBe('account unknown')
    expect(screen.getByTestId('claude-usage-account')).toHaveAttribute('data-state', 'unknown')
    expect(screen.getByTestId('claude-usage').getAttribute('title')).toContain('Claude account unknown (expired)')
    expect(screen.queryByText('russell@example.com')).toBeNull()
  })

  it('a server without the account field is shown as unknown (old backend, new frontend)', async () => {
    vi.spyOn(usageApi, 'fetchClaudeUsage').mockResolvedValue(ok(10, 20, { account: undefined }))
    render(<ClaudeUsageMeter />)
    await flush()
    expect(screen.getByTestId('claude-usage-email').textContent).toBe('account unknown')
  })

  it('shows "usage n/a" with the reason when unavailable, and when the fetch throws; account still shown', async () => {
    const spy = vi.spyOn(usageApi, 'fetchClaudeUsage').mockResolvedValue({ available: false, reason: 'http_429', account: acct() })
    const { unmount } = render(<ClaudeUsageMeter />)
    await flush()
    expect(screen.getByTestId('claude-usage-na').textContent).toBe('usage n/a')
    expect(screen.getByTestId('claude-usage-email').textContent).toBe('russell@example.com')
    expect(screen.getByTestId('claude-usage').getAttribute('title')).toContain('http_429')
    unmount()
    spy.mockRejectedValue(new Error('boom'))
    render(<ClaudeUsageMeter />)
    await flush()
    expect(screen.getByTestId('claude-usage-na').textContent).toBe('usage n/a')
    expect(screen.getByTestId('claude-usage').getAttribute('title')).toContain('network')
    expect(screen.getByTestId('claude-usage-email').textContent).toBe('account unknown')
  })

  it('polls every 5 minutes, retries after 1 minute while unavailable, and stops after unmount', async () => {
    const spy = vi.spyOn(usageApi, 'fetchClaudeUsage').mockResolvedValue(ok(10, 10))
    const { unmount } = render(<ClaudeUsageMeter />)
    await flush()
    expect(spy).toHaveBeenCalledTimes(1)
    await act(async () => { vi.advanceTimersByTime(USAGE_POLL_MS - 1000) })
    expect(spy).toHaveBeenCalledTimes(1)
    await act(async () => { vi.advanceTimersByTime(1000) })
    expect(spy).toHaveBeenCalledTimes(2)
    spy.mockResolvedValue({ available: false, reason: 'http_429' })
    await act(async () => { vi.advanceTimersByTime(USAGE_POLL_MS) })
    expect(spy).toHaveBeenCalledTimes(3)
    await act(async () => { vi.advanceTimersByTime(USAGE_RETRY_MS) })      // unavailable -> look again in 60 s
    expect(spy).toHaveBeenCalledTimes(4)
    unmount()
    await act(async () => { vi.advanceTimersByTime(USAGE_POLL_MS * 2) })
    expect(spy).toHaveBeenCalledTimes(4)
  })

  it('refetches when a sleeping tab becomes visible again', async () => {
    const spy = vi.spyOn(usageApi, 'fetchClaudeUsage').mockResolvedValue(ok(10, 10))
    render(<ClaudeUsageMeter />)
    await flush()
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')) })
    expect(spy).toHaveBeenCalledTimes(2)
  })

  it('last-good numbers while the usage API is busy are marked stale (~) and re-polled every minute', async () => {
    const spy = vi.spyOn(usageApi, 'fetchClaudeUsage').mockResolvedValue(
      { ...(ok(40, 60) as Extract<usageApi.ClaudeUsage, { available: true }>), stale: { reason: 'http_429', since: '2026-10-28T16:00:00+00:00' } },
    )
    render(<ClaudeUsageMeter />)
    await flush()
    expect(screen.getByTestId('claude-usage-5h').textContent).toContain('5h ~40%')
    expect(screen.getByTestId('claude-usage').getAttribute('title')).toContain('usage API http_429')
    expect(screen.getByTestId('claude-usage-email').textContent).toBe('russell@example.com')
    expect(spy).toHaveBeenCalledTimes(1)
    await act(async () => { vi.advanceTimersByTime(USAGE_RETRY_MS) })
    expect(spy).toHaveBeenCalledTimes(2)
  })

  describe('account change flag', () => {
    it('first account seen becomes the baseline (no flag); a later different account is flagged until acknowledged', async () => {
      const spy = vi.spyOn(usageApi, 'fetchClaudeUsage').mockResolvedValue(ok(10, 20))
      render(<ClaudeUsageMeter />)
      await flush()
      expect(screen.queryByTestId('claude-usage-changed')).toBeNull()
      expect(readSeen()?.key).toBe('aaaaaaaaaaaa')
      spy.mockResolvedValue(ok(10, 20, { account: acct({ email: 'corey@example.com', key: 'bbbbbbbbbbbb' }) }))
      await act(async () => { vi.advanceTimersByTime(USAGE_POLL_MS) })
      await flush()
      expect(screen.getByTestId('claude-usage-email').textContent).toBe('corey@example.com')
      expect(screen.getByTestId('claude-usage-changed').textContent).toBe('account changed')
      expect(screen.getByTestId('claude-usage-account')).toHaveAttribute('data-state', 'changed')
      expect(screen.getByTestId('claude-usage').getAttribute('title')).toContain('was russell@example.com')
      expect(readSeen()?.key).toBe('aaaaaaaaaaaa')            // a switch does not move the baseline
      fireEvent.click(screen.getByTestId('claude-usage'))
      expect(screen.getByTestId('claude-usage-pop-change').textContent).toContain('was russell@example.com')
      fireEvent.click(screen.getByTestId('claude-usage-ack'))
      expect(screen.queryByTestId('claude-usage-changed')).toBeNull()
      expect(readSeen()?.key).toBe('bbbbbbbbbbbb')
    })

    it('a switch made while the device was away is flagged on the next load (persisted baseline)', async () => {
      localStorage.setItem(ACCOUNT_SEEN_KEY, JSON.stringify({ key: 'aaaaaaaaaaaa', email: 'russell@example.com', ackAt: null }))
      vi.spyOn(usageApi, 'fetchClaudeUsage').mockResolvedValue(ok(10, 20, { account: acct({ email: 'corey@example.com', key: 'bbbbbbbbbbbb' }) }))
      render(<ClaudeUsageMeter />)
      await flush()
      expect(screen.getByTestId('claude-usage-changed')).toBeInTheDocument()
    })

    it('a server-side change flag shows on a device that never saw the old account', async () => {
      vi.spyOn(usageApi, 'fetchClaudeUsage').mockResolvedValue(
        ok(10, 20, { account: acct({ changed: { at: '2026-10-28T15:30:00+00:00', previous_email: 'prev@example.com' } }) }),
      )
      render(<ClaudeUsageMeter />)
      await flush()
      expect(screen.getByTestId('claude-usage-changed')).toBeInTheDocument()
      expect(screen.getByTestId('claude-usage').getAttribute('title')).toContain('was prev@example.com')
    })

    it('same account reappearing never raises a flag, and a changed email on the same account just updates the baseline', async () => {
      const spy = vi.spyOn(usageApi, 'fetchClaudeUsage').mockResolvedValue(ok(10, 20))
      render(<ClaudeUsageMeter />)
      await flush()
      spy.mockResolvedValue(ok(10, 20, { account: acct({ email: 'russell.new@example.com' }) }))
      await act(async () => { vi.advanceTimersByTime(USAGE_POLL_MS) })
      await flush()
      expect(screen.queryByTestId('claude-usage-changed')).toBeNull()
      expect(readSeen()?.email).toBe('russell.new@example.com')
    })
  })

  describe('details popover (the tooltip is unreachable on a touch screen)', () => {
    it('tap opens the details; tap outside and Escape close them', async () => {
      vi.spyOn(usageApi, 'fetchClaudeUsage').mockResolvedValue(ok(48, 39))
      render(<ClaudeUsageMeter />)
      await flush()
      expect(screen.queryByTestId('claude-usage-popover')).toBeNull()
      fireEvent.click(screen.getByTestId('claude-usage'))
      const pop = screen.getByTestId('claude-usage-popover')
      expect(pop.textContent).toContain('russell@example.com')
      expect(pop.textContent).toContain('Max 20x')
      expect(pop.textContent).toContain('5h: 48%'.replace('5h: ', ''))
      fireEvent.keyDown(document, { key: 'Escape' })
      expect(screen.queryByTestId('claude-usage-popover')).toBeNull()
      fireEvent.click(screen.getByTestId('claude-usage'))
      expect(screen.getByTestId('claude-usage-popover')).toBeInTheDocument()
      fireEvent.pointerDown(document.body)
      expect(screen.queryByTestId('claude-usage-popover')).toBeNull()
    })

    it('the popover states plainly when the account is unknown', async () => {
      vi.spyOn(usageApi, 'fetchClaudeUsage').mockResolvedValue(ok(10, 10, { account: { available: false, reason: 'network' } }))
      render(<ClaudeUsageMeter />)
      await flush()
      fireEvent.click(screen.getByTestId('claude-usage'))
      expect(screen.getByTestId('claude-usage-pop-account').textContent).toBe('unknown (network)')
    })
  })
})

describe('Header placement (Russell 2026-10-28: left of Reconnect Claude)', () => {
  it('renders the usage meter before the Reconnect Claude button', () => {
    const meter = headerSrc.indexOf('<ClaudeUsageMeter />')
    const reconnect = headerSrc.indexOf('<ReconnectClaudeButton />')
    expect(meter).toBeGreaterThan(-1)
    expect(reconnect).toBeGreaterThan(-1)
    expect(meter).toBeLessThan(reconnect)
  })
})


// Source-level guard for the phone defect. The real check is e2e/header-meter-check.mjs (WebKit + Chromium at
// 360-430 px, portrait + landscape); this one runs in `npm test` and fails the moment someone hides the meter or
// shrinks it back to the faint 10px text that Russell could not see on his phone (2026-10-28).
// vitest runs from the react-portal package root (jsdom makes import.meta.url an http URL)
const readCss = (name: string) => readFileSync(resolve(process.cwd(), 'src/components/layout', name), 'utf8')
const meterCss = readCss('ClaudeUsageMeter.css')
const headerCss = readCss('Header.css')

/** [selector, body] for every rule (media wrappers and comments stripped). */
function rules(css: string): Array<[string, string]> {
  const flat = css.replace(/\/\*[\s\S]*?\*\//g, '').replace(/@media[^{]*\{/g, '')
  return flat.split('}').map(chunk => {
    const i = chunk.indexOf('{')
    return i < 0 ? null : ([chunk.slice(0, i).trim(), chunk.slice(i + 1)] as [string, string])
  }).filter((r): r is [string, string] => !!r && !!r[0])
}

describe('phone layout contract (CSS)', () => {
  it('only the plan label may ever be display:none (it moves into the tap-for-details card); never the meter, account or usage text', () => {
    const hidden = rules(meterCss).filter(([, body]) => /display:\s*none/.test(body)).map(([sel]) => sel)
    expect(hidden).toEqual(['.claude-usage-plan'])
  })

  it('no text under 11px and no faint tertiary grey', () => {
    const sizes = [...meterCss.matchAll(/font-size:\s*(\d+(?:\.\d+)?)px/g)].map(m => Number(m[1]))
    expect(sizes.length).toBeGreaterThan(3)
    for (const size of sizes) expect(size).toBeGreaterThanOrEqual(11)
    expect(meterCss).not.toContain('text-tertiary')
  })

  it('phones get a second header row for the meter instead of squeezing it into the first', () => {
    const header = rules(headerCss)
    expect(header.some(([sel, body]) => sel === '.header-right' && /display:\s*contents/.test(body))).toBe(true)
    expect(header.some(([sel, body]) => sel === '.header' && /flex-wrap:\s*wrap/.test(body))).toBe(true)
    expect(rules(meterCss).some(([sel, body]) => sel === '.claude-usage-wrap' && /flex:\s*0 0 100%/.test(body))).toBe(true)
  })

  it('the status LABEL may go on a narrow phone, the status dot never', () => {
    expect(rules(headerCss).some(([sel, body]) => sel === '.header .status-label' && /display:\s*none/.test(body))).toBe(true)
    expect(headerCss).not.toMatch(/status-dot[^}]*display:\s*none/)
  })
})
