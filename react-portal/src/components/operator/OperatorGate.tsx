import { useState } from 'react'
import { useOperatorStore } from '../../stores/operatorStore'
import { OPERATOR_OPTIONS } from '../../utils/constants'
import './OperatorGate.css'

/**
 * P20 operator identity gate — CONFIRM ONCE PER SESSION, then DISAPPEAR.
 *
 * On each new session (fresh page load / reload) no operator is chosen yet
 * (the store is session-scoped, in-memory), so this shows a ONE-TIME blocking
 * confirm: "Who's at the portal?". The full-screen overlay means the human
 * cannot start typing until they pick themselves — so no message is ever sent
 * un-tagged. The choice is tagged onto every message (ChatView ->
 * send(text, operator)) so Primary always knows who is speaking.
 *
 * After the one tap this renders NOTHING — no persistent pill, no floating
 * "Speaking as X" over the input, no "change" affordance. The input area is
 * fully clear. There is NO mid-session switch UI by design (Russell 2026-09-07:
 * "zero chance it will ever need to be changed on a physical device since we're
 * all in different places"). Re-confirmation happens naturally at the start of
 * the next session/reload.
 *
 * NOTE: this is a UI-visibility change only. Identity is still selected and
 * still rides on every outbound message (P20 invariant) — only the persistent
 * on-screen pill was removed.
 */
export function OperatorGate() {
  const operator = useOperatorStore(s => s.operator)
  const setOperator = useOperatorStore(s => s.setOperator)
  const [other, setOther] = useState('')

  const choose = (name: string) => {
    const clean = name.trim()
    if (!clean) return
    setOperator(clean)
    setOther('')
  }

  // Once chosen for this session, render nothing at all — the pill is gone
  // completely and the chat input is fully clear.
  if (operator) return null

  return (
    <div className="op-overlay" role="dialog" aria-modal="true" aria-label="Select operator">
      <div className="op-modal">
        <h2 className="op-title">Who's at the portal?</h2>
        <p className="op-sub">
          Your name is tagged onto every message so Witness always knows who is
          speaking. Pick yourself — just once for this session:
        </p>
        <div className="op-options">
          {OPERATOR_OPTIONS.map(name => (
            <button
              key={name}
              className="op-option"
              onClick={() => choose(name)}
            >
              {name}
            </button>
          ))}
        </div>
        <div className="op-other">
          <input
            className="op-other-input"
            type="text"
            placeholder="Someone else… type a name"
            value={other}
            maxLength={40}
            onChange={e => setOther(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') choose(other) }}
          />
          <button
            className="op-other-btn"
            disabled={!other.trim()}
            onClick={() => choose(other)}
          >
            Use
          </button>
        </div>
      </div>
    </div>
  )
}
