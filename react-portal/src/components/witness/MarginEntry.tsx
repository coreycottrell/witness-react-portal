/**
 * MarginEntry v3 — single entry card with markdown, haiku detection,
 * collapsible long content, author badge, timestamp.
 */
import { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

const COLLAPSE_THRESHOLD = 250

interface MarginEntryProps {
  content: string
  timestamp: string
  author: 'primary' | 'corey'
  boopId?: string
}

function RelativeTime({ ts }: { ts: string }) {
  try {
    const d = new Date(ts)
    const now = new Date()
    const diffMs = now.getTime() - d.getTime()
    const diffMins = Math.floor(diffMs / 60000)
    const diffHrs = Math.floor(diffMs / 3600000)
    const diffDays = Math.floor(diffMs / 86400000)

    let relative: string
    if (diffMins < 1) relative = 'just now'
    else if (diffMins < 60) relative = `${diffMins}m ago`
    else if (diffHrs < 24) relative = `${diffHrs}h ago`
    else if (diffDays < 7) relative = `${diffDays}d ago`
    else relative = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })

    const full = d.toLocaleString(undefined, {
      weekday: 'short', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })

    return <time className="margin-entry-time" title={full}>{relative}</time>
  } catch {
    return <time className="margin-entry-time">{ts}</time>
  }
}

function EntryTime({ ts }: { ts: string }) {
  try {
    const d = new Date(ts)
    return (
      <span className="margin-entry-clock">
        {d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}
      </span>
    )
  } catch {
    return null
  }
}

export function MarginEntry({ content, timestamp, author, boopId }: MarginEntryProps) {
  const [expanded, setExpanded] = useState(false)
  const isLong = content.length > COLLAPSE_THRESHOLD
  const displayContent = isLong && !expanded
    ? content.slice(0, COLLAPSE_THRESHOLD) + '...'
    : content

  const authorLabel = author === 'primary' ? 'Witness' : 'Corey'
  const cardClass = `margin-entry-card margin-entry-card--${author === 'primary' ? 'witness' : 'corey'}`

  return (
    <article className={cardClass}>
      <div className="margin-entry-meta">
        <span className={`margin-author-badge margin-author-badge--${author === 'primary' ? 'witness' : 'corey'}`}>
          {authorLabel}
        </span>
        <EntryTime ts={timestamp} />
        {boopId && !boopId.startsWith('ts-') && (
          <span className="margin-boop-tag">BOOP #{boopId}</span>
        )}
        <RelativeTime ts={timestamp} />
      </div>
      <div className="margin-entry-body">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>
          {displayContent}
        </ReactMarkdown>
      </div>
      {isLong && (
        <button
          className="margin-expand-btn"
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? 'Show less' : 'Show more'}
        </button>
      )}
    </article>
  )
}
