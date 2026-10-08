import { create } from 'zustand'
import { OPERATOR_KEY } from '../utils/constants'

/**
 * P20 operator identity — PERSISTED (confirm-once, then never again).
 *
 * Holds WHO is at this portal tab (Corey / Russell / Jared / other). The name
 * is tagged onto every message (ChatView -> send(text, operator)) so Primary
 * always knows who is speaking, and so every operator's screen renders correct
 * attribution.
 *
 * CHANGE 2026-09-15 (Russell directive, "Who's at the portal?" pop-up removal):
 * the identity is now PERSISTED to localStorage (OPERATOR_KEY) instead of being
 * session-scoped in-memory. On a fresh page load / reload the store re-hydrates
 * the previously-chosen operator from localStorage, so the OperatorGate sees a
 * non-null operator and renders NOTHING — the "Who's at the portal?" modal no
 * longer pops up on every entry/refresh. It appears exactly ONCE, on a device
 * that has never picked (nothing stored), and never again after that pick.
 *
 * Attribution is fully preserved: `operator` still drives send(text, operator)
 * -> POST /api/chat/send {sender}. Only the trigger for the gate changed
 * (persisted vs in-memory) — nothing about how the name rides on messages.
 */
interface OperatorState {
  operator: string | null
  setOperator: (name: string) => void
  clearOperator: () => void
}

// Same sanitize as setOperator, applied to whatever was persisted so a hand-
// edited/corrupt localStorage value can never inject anything.
function sanitize(name: string): string {
  return name.replace(/[^\w .-]/g, '').slice(0, 40).trim()
}

function loadPersisted(): string | null {
  try {
    const raw = localStorage.getItem(OPERATOR_KEY)
    if (!raw) return null
    const clean = sanitize(raw)
    return clean || null
  } catch {
    return null
  }
}

export const useOperatorStore = create<OperatorState>((set) => ({
  // Re-hydrate from localStorage so a returning device is already identified and
  // the gate never re-prompts.
  operator: loadPersisted(),

  setOperator: (name: string) => {
    const clean = sanitize(name)
    if (!clean) return
    try { localStorage.setItem(OPERATOR_KEY, clean) } catch { /* ignore */ }
    set({ operator: clean })
  },

  clearOperator: () => {
    try { localStorage.removeItem(OPERATOR_KEY) } catch { /* ignore */ }
    set({ operator: null })
  },
}))
