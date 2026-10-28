import { apiGet } from './client'

/** One Claude plan window as returned by GET /api/usage/claude (whitelisted by the server). */
export interface UsageWindow {
  utilization: number | null
  resets_at: string | null
  active: boolean
}

/** Which Claude account is signed in (read server-side from Claude's own profile endpoint). */
export type ClaudeAccount =
  | {
      available: true
      email: string | null
      name: string | null
      plan: string | null
      tier: string | null
      /** short one-way fingerprint of the account id; changes when a different account signs in */
      key: string
      /** set by the server for a while after it saw the account differ from its previous read */
      changed: { at: string; previous_email: string | null } | null
    }
  | { available: false; reason: string }

export type ClaudeUsage =
  | {
      available: true
      five_hour: UsageWindow
      seven_day: UsageWindow
      plan: string | null
      fetched_at: string
      /** set while the usage API is rate-limiting/unreachable: these are the LAST GOOD numbers (fetched_at), not live */
      stale?: { reason: string; since: string | null }
      /** absent on a server that predates the account display: treated as unknown */
      account?: ClaudeAccount
    }
  | { available: false; reason: string; account?: ClaudeAccount }

export function fetchClaudeUsage(): Promise<ClaudeUsage> {
  return apiGet<ClaudeUsage>('/api/usage/claude')
}
