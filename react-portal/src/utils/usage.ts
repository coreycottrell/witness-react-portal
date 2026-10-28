import type { ClaudeAccount, UsageWindow } from '../api/usage'

/** Poll every 5 min (the server caches 60 s). Also refetch when a sleeping tab wakes. */
export const USAGE_POLL_MS = 5 * 60 * 1000
/** While the last read failed (e.g. the usage API rate-limited us) look again sooner. */
export const USAGE_RETRY_MS = 60 * 1000

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

/** "max" + "20x" -> "Max 20x"; unknown plan -> null (never a guess). */
export function planLabel(plan: string | null | undefined, tier: string | null | undefined): string | null {
  if (!plan) return null
  const name = plan.charAt(0).toUpperCase() + plan.slice(1)
  return tier ? `${name} ${tier}` : name
}

// ── "Which account is this?" change detection ────────────────────────────────────────────
// The account is switched often and the meter shows whichever one is signed in, so a switch must
// be impossible to miss. Two sources, either one raises the flag:
//   * the server saw the account differ from its previous read (visible on every device, for a while);
//   * THIS device last looked at a different account (persisted, so a phone opened hours later still
//     says "changed since you last looked" until it is acknowledged).

export const ACCOUNT_SEEN_KEY = 'aiciv-claude-account-seen'

export interface SeenAccount {
  key: string
  email: string | null
  /** ISO time of the last acknowledgement on this device; server flags older than this are hidden */
  ackAt: string | null
}

export interface AccountChange {
  previousEmail: string | null
  source: 'device' | 'server'
}

export function readSeen(): SeenAccount | null {
  try {
    const raw = localStorage.getItem(ACCOUNT_SEEN_KEY)
    if (!raw) return null
    const v = JSON.parse(raw)
    if (!v || typeof v.key !== 'string' || !v.key) return null
    return {
      key: v.key,
      email: typeof v.email === 'string' ? v.email : null,
      ackAt: typeof v.ackAt === 'string' ? v.ackAt : null,
    }
  } catch {
    return null
  }
}

export function writeSeen(seen: SeenAccount): void {
  try { localStorage.setItem(ACCOUNT_SEEN_KEY, JSON.stringify(seen)) } catch { /* storage blocked: flag just resets next load */ }
}

/** Only called with an available account. Pure: no storage access (the caller persists). */
export function detectChange(
  account: Extract<ClaudeAccount, { available: true }>,
  seen: SeenAccount | null,
): AccountChange | null {
  if (seen && seen.key !== account.key) return { previousEmail: seen.email, source: 'device' }
  if (account.changed) {
    const at = Date.parse(account.changed.at)
    const ack = seen?.ackAt ? Date.parse(seen.ackAt) : NaN
    if (!Number.isNaN(at) && !(Number.isFinite(ack) && ack >= at)) {
      return { previousEmail: account.changed.previous_email, source: 'server' }
    }
  }
  return null
}
