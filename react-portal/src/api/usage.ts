import { apiGet } from './client'

/** One Claude plan window as returned by GET /api/usage/claude (whitelisted by the server). */
export interface UsageWindow {
  utilization: number | null
  resets_at: string | null
  active: boolean
}

export type ClaudeUsage =
  | {
      available: true
      five_hour: UsageWindow
      seven_day: UsageWindow
      plan: string | null
      fetched_at: string
    }
  | { available: false; reason: string }

export function fetchClaudeUsage(): Promise<ClaudeUsage> {
  return apiGet<ClaudeUsage>('/api/usage/claude')
}
