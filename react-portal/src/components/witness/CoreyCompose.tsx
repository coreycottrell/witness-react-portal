/**
 * CoreyCompose — text area with guide questions, BOOP targeting, submit.
 */
import { useState, useEffect } from 'react'
import type { BoopRow } from './MarginRow'

interface CoreyComposeProps {
  boopRows: BoopRow[]
  onPost: (content: string, boopId: string) => Promise<void>
  targetBoopId?: string
  onTargetChange?: (boopId: string) => void
}

/** Extract questions from Witness's latest entry. */
function extractQuestions(content: string): string[] {
  const questions: string[] = []
  const lines = content.split('\n')
  for (const line of lines) {
    const trimmed = line.trim()
    // Lines ending with ? or wrapped in *...*? or starting with "Question"
    if (
      trimmed.endsWith('?') ||
      (trimmed.startsWith('*') && trimmed.endsWith('?*')) ||
      trimmed.toLowerCase().startsWith('question')
    ) {
      // Clean up markdown formatting
      const clean = trimmed.replace(/^\*+/, '').replace(/\*+$/, '').replace(/^#+\s*/, '').trim()
      if (clean.length > 5) questions.push(clean)
    }
  }
  return questions
}

export function CoreyCompose({ boopRows, onPost, targetBoopId, onTargetChange }: CoreyComposeProps) {
  const [text, setText] = useState('')
  const [posting, setPosting] = useState(false)
  const [selectedBoop, setSelectedBoop] = useState(targetBoopId || '')

  // Update selected boop when target changes externally (e.g., reply button)
  useEffect(() => {
    if (targetBoopId) setSelectedBoop(targetBoopId)
  }, [targetBoopId])

  // Default to latest boop
  useEffect(() => {
    if (!selectedBoop && boopRows.length > 0) {
      setSelectedBoop(boopRows[0].boop_id)
    }
  }, [boopRows, selectedBoop])

  const currentRow = boopRows.find(r => r.boop_id === selectedBoop)
  const latestWitness = currentRow?.witnessEntries?.[0]
  const questions = latestWitness
    ? extractQuestions(latestWitness.content)
    : []

  const handlePost = async () => {
    const trimmed = text.trim()
    if (!trimmed || posting) return
    setPosting(true)
    try {
      await onPost(trimmed, selectedBoop)
      setText('')
    } finally {
      setPosting(false)
    }
  }

  const handleBoopChange = (boopId: string) => {
    setSelectedBoop(boopId)
    onTargetChange?.(boopId)
  }

  return (
    <div className="margin-compose">
      {/* BOOP selector */}
      {boopRows.length > 1 && (
        <div className="margin-compose__boop-select">
          <label className="margin-compose__label">Responding to:</label>
          <select
            className="margin-compose__select"
            value={selectedBoop}
            onChange={e => handleBoopChange(e.target.value)}
          >
            {boopRows.map(r => (
              <option key={r.boop_id} value={r.boop_id}>
                {r.boop_id.startsWith('ts-')
                  ? new Date(r.timestamp).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
                  : `BOOP #${r.boop_id}`}
              </option>
            ))}
          </select>
        </div>
      )}

      {/* Guide questions */}
      {questions.length > 0 && (
        <div className="margin-compose__guide">
          {questions.map((q, i) => (
            <p key={i} className="margin-compose__question">
              Witness asked: &ldquo;{q}&rdquo;
            </p>
          ))}
        </div>
      )}

      {/* Text area */}
      <textarea
        className="margin-input"
        placeholder="What's on your mind, Corey?"
        rows={4}
        value={text}
        onChange={e => setText(e.target.value)}
        onKeyDown={e => {
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) handlePost()
        }}
      />

      <div className="margin-compose-footer">
        <span className="margin-hint">Ctrl+Enter to post</span>
        <button
          className="margin-post-btn"
          onClick={handlePost}
          disabled={posting || !text.trim()}
        >
          {posting ? 'Posting...' : 'Post to Margin'}
        </button>
      </div>
    </div>
  )
}
