import type { UsageWindow } from '../api/usage'

/** Poll every 5 min (the server caches 60 s). Also refetch when a sleeping tab wakes. */
export const USAGE_POLL_MS = 5 * 60 * 1000

export type UsageLevel = 'ok' | 'warn' | 'crit'

export function usageLevel(pct: number): UsageLevel {
  if (pct >= 90) return 'crit'
  if (pct >= 70) return 'warn'
  return 'ok'
}

export function usagePct(win: UsageWindow | undefined): number | null {
  const u = win && typeof win.utilization === 'number' ? win.utilization : null
  if (u === null || !Number.isFinite(u)) return null
  return Math.max(0, Math.min(100, Math.round(u)))
}
