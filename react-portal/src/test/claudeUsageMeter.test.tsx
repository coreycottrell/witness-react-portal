import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import { ClaudeUsageMeter } from '../components/layout/ClaudeUsageMeter'
import { usageLevel, usagePct, USAGE_POLL_MS } from '../utils/usage'
import * as usageApi from '../api/usage'

const ok = (five: number | null, week: number | null, extra: Partial<{ fiveActive: boolean; plan: string | null }> = {}) => ({
  available: true as const,
  five_hour: { utilization: five, resets_at: '2026-10-28T17:00:00+00:00', active: extra.fiveActive ?? true },
  seven_day: { utilization: week, resets_at: '2026-11-01T15:00:00+00:00', active: true },
  plan: extra.plan === undefined ? 'max' : extra.plan,
  fetched_at: '2026-10-28T16:00:00+00:00',
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

describe('ClaudeUsageMeter', () => {
  beforeEach(() => { vi.useFakeTimers() })
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
    expect(title).toContain('Plan: max')
  })

  it('an inactive window shows 0% and "no active window"', async () => {
    vi.spyOn(usageApi, 'fetchClaudeUsage').mockResolvedValue(ok(0, 10, { fiveActive: false }))
    render(<ClaudeUsageMeter />)
    await flush()
    expect(screen.getByTestId('claude-usage-5h')).toHaveAttribute('data-level', 'ok')
    expect(screen.getByTestId('claude-usage').getAttribute('title')).toContain('5h: no active window')
  })

  it('shows "usage n/a" with the reason when unavailable, and when the fetch throws', async () => {
    const spy = vi.spyOn(usageApi, 'fetchClaudeUsage').mockResolvedValue({ available: false, reason: 'expired' })
    const { unmount } = render(<ClaudeUsageMeter />)
    await flush()
    expect(screen.getByTestId('claude-usage').textContent).toBe('usage n/a')
    expect(screen.getByTestId('claude-usage').getAttribute('title')).toContain('expired')
    unmount()
    spy.mockRejectedValue(new Error('boom'))
    render(<ClaudeUsageMeter />)
    await flush()
    expect(screen.getByTestId('claude-usage').textContent).toBe('usage n/a')
    expect(screen.getByTestId('claude-usage').getAttribute('title')).toContain('network')
  })

  it('polls every 5 minutes and stops after unmount', async () => {
    const spy = vi.spyOn(usageApi, 'fetchClaudeUsage').mockResolvedValue(ok(10, 10))
    const { unmount } = render(<ClaudeUsageMeter />)
    await flush()
    expect(spy).toHaveBeenCalledTimes(1)
    await act(async () => { vi.advanceTimersByTime(USAGE_POLL_MS) })
    expect(spy).toHaveBeenCalledTimes(2)
    unmount()
    await act(async () => { vi.advanceTimersByTime(USAGE_POLL_MS * 2) })
    expect(spy).toHaveBeenCalledTimes(2)
  })

  it('refetches when a sleeping tab becomes visible again', async () => {
    const spy = vi.spyOn(usageApi, 'fetchClaudeUsage').mockResolvedValue(ok(10, 10))
    render(<ClaudeUsageMeter />)
    await flush()
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')) })
    expect(spy).toHaveBeenCalledTimes(2)
  })
})
