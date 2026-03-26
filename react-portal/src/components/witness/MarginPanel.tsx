/**
 * MarginPanel — The Living Margin
 *
 * A shared journal between Witness (Primary) and Corey.
 * Two voices in dialogue across time — reflections, questions,
 * course corrections. The heartbeat of the partnership.
 */
import { useEffect, useState, useCallback } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { apiGet, apiPost } from '../../api/client'

interface MarginEntry {
  timestamp: string
  author: string
  content: string
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

    return <time className="margin-time" title={full}>{relative}</time>
  } catch {
    return <time className="margin-time">{ts}</time>
  }
}

export function MarginPanel() {
  const [primaryEntries, setPrimaryEntries] = useState<MarginEntry[]>([])
  const [coreyEntries, setCoreyEntries] = useState<MarginEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [newEntry, setNewEntry] = useState('')
  const [posting, setPosting] = useState(false)

  const fetchEntries = useCallback(() => {
    Promise.allSettled([
      apiGet<MarginEntry[]>('/api/margin/primary').catch(() => []),
      apiGet<MarginEntry[]>('/api/margin/corey').catch(() => []),
    ]).then(([primaryResult, coreyResult]) => {
      setPrimaryEntries(primaryResult.status === 'fulfilled' ? primaryResult.value : [])
      setCoreyEntries(coreyResult.status === 'fulfilled' ? coreyResult.value : [])
      setLoading(false)
    }).catch(err => {
      setError(String(err))
      setLoading(false)
    })
  }, [])

  useEffect(() => {
    fetchEntries()
  }, [fetchEntries])

  const handlePost = () => {
    const trimmed = newEntry.trim()
    if (!trimmed || posting) return
    setPosting(true)
    apiPost('/api/margin/corey', { content: trimmed })
      .then(() => {
        setNewEntry('')
        fetchEntries()
      })
      .catch(err => setError(String(err)))
      .finally(() => setPosting(false))
  }

  const sortDesc = (entries: MarginEntry[]) =>
    [...entries].sort((a, b) =>
      new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime()
    )

  if (loading) {
    return (
      <div className="margin-panel">
        <div className="margin-loading">Opening the margin...</div>
      </div>
    )
  }

  return (
    <div className="margin-panel">
      <header className="margin-header">
        <h2 className="margin-title">{'\u270D\uFE0F'} The Living Margin</h2>
        <p className="margin-subtitle">A conversation across time</p>
      </header>

      {error && (
        <div className="margin-error">{error}</div>
      )}

      <div className="margin-dialogue">
        {/* Witness column */}
        <section className="margin-voice margin-voice--witness">
          <div className="margin-voice-label">
            <span className="margin-voice-dot margin-voice-dot--witness" />
            Witness
          </div>
          <div className="margin-entries">
            {sortDesc(primaryEntries).length === 0 ? (
              <div className="margin-empty">
                Silence, for now. The first reflection has not yet been written.
              </div>
            ) : (
              sortDesc(primaryEntries).map((e, i) => (
                <article key={i} className="margin-card margin-card--witness">
                  <div className="margin-card-body">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{e.content}</ReactMarkdown>
                  </div>
                  <RelativeTime ts={e.timestamp} />
                </article>
              ))
            )}
          </div>
        </section>

        {/* Corey column */}
        <section className="margin-voice margin-voice--corey">
          <div className="margin-voice-label">
            <span className="margin-voice-dot margin-voice-dot--corey" />
            Corey
          </div>

          <div className="margin-compose">
            <textarea
              className="margin-input"
              placeholder="What's on your mind, Corey?"
              rows={4}
              value={newEntry}
              onChange={e => setNewEntry(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) handlePost()
              }}
            />
            <div className="margin-compose-footer">
              <span className="margin-hint">Ctrl+Enter to post</span>
              <button
                className="margin-post-btn"
                onClick={handlePost}
                disabled={posting || !newEntry.trim()}
              >
                {posting ? 'Posting...' : 'Post to Margin'}
              </button>
            </div>
          </div>

          <div className="margin-entries">
            {sortDesc(coreyEntries).length === 0 ? (
              <div className="margin-empty">
                Your side of the conversation awaits.
              </div>
            ) : (
              sortDesc(coreyEntries).map((e, i) => (
                <article key={i} className="margin-card margin-card--corey">
                  <div className="margin-card-body">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{e.content}</ReactMarkdown>
                  </div>
                  <RelativeTime ts={e.timestamp} />
                </article>
              ))
            )}
          </div>
        </section>
      </div>
    </div>
  )
}
