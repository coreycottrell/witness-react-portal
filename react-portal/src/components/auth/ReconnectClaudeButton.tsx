/**
 * ReconnectClaudeButton — a always-visible "Reconnect Claude" control.
 *
 * WHY THIS EXISTS (ticket 2153 root cause):
 * /api/auth/status trusts "tmux session alive" as authenticated:true, so the
 * ClaudeAuthFlow re-auth modal (which only renders when authenticated===false)
 * NEVER surfaces on a live CIV. When a Claude OAuth token expires but the
 * session is still up, or a human simply wants to switch/refresh the Claude
 * login, there is no way to re-trigger auth. This button fixes that: it fires
 * a `claude:reconnect` window event that ClaudeAuthFlow listens for and opens
 * on demand, regardless of the reported auth status.
 *
 * Self-contained: no new deps, no store wiring. Drop it anywhere that is
 * always mounted (e.g. the Header) and pair it with the ClaudeAuthFlow patch.
 */

/** Event name shared with ClaudeAuthFlow. Keep these identical. */
export const RECONNECT_EVENT = 'claude:reconnect'

export function ReconnectClaudeButton() {
  const onClick = () => {
    const ok = window.confirm(
      'Reconnect Claude?\n\nThis opens the Claude sign-in flow so you can log ' +
        'back in (e.g. after the Claude login expired). It does NOT touch your ' +
        'memory, identity, or files — only the Claude login.',
    )
    if (!ok) return
    window.dispatchEvent(new CustomEvent(RECONNECT_EVENT))
  }

  return (
    <button
      type="button"
      className="reconnect-claude-btn"
      onClick={onClick}
      title="Re-open the Claude sign-in flow (fixes an expired Claude login)"
      style={{
        background: 'transparent',
        border: '1px solid var(--border-color, #3a3a3a)',
        color: 'var(--text-secondary, #cfcfcf)',
        borderRadius: '8px',
        padding: '6px 12px',
        fontSize: '13px',
        fontWeight: 500,
        cursor: 'pointer',
        whiteSpace: 'nowrap',
      }}
    >
      Reconnect Claude
    </button>
  )
}
