/**
 * MarginPanel — The Living Margin (v3)
 *
 * A shared journal between Witness (Primary) and Corey.
 * Rebuilt: single-column timeline, day grouping, pagination,
 * collapsible long entries.
 */
import { useEffect, useState, useCallback, useRef } from 'react'
import { apiGet, apiPost } from '../../api/client'
import './witness.css'
import { PointsSummary } from './PointsSummary'
import { CoreyCompose } from './CoreyCompose'
import { MarginEntry } from './MarginEntry'

const PAGE_SIZE = 30
const REFRESH_INTERVAL = 60_000

export interface MarginEntryData {
  timestamp: string
  author: 'primary' | 'corey'
  content: string
  boop_id?: string
}

interface DayGroup {
  dateLabel: string
  entries: MarginEntryData[]
}

function formatDayLabel(dateStr: string): string {
  const d = new Date(dateStr)
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const entry = new Date(d.getFullYear(), d.getMonth(), d.getDate())
  const diffDays = Math.round((today.getTime() - entry.getTime()) / 86400000)

  if (diffDays === 0) return 'Today'
  if (diffDays === 1) return 'Yesterday'
  if (diffDays < 7) return d.toLocaleDateString(undefined, { weekday: 'long' })
  return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })
}

function dateKey(ts: string): string {
  const d = new Date(ts)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

function groupByDay(entries: MarginEntryData[]): DayGroup[] {
  const map = new Map<string, MarginEntryData[]>()

  for (const entry of entries) {
    const key = dateKey(entry.timestamp)
    if (!map.has(key)) map.set(key, [])
    map.get(key)!.push(entry)
  }

  const groups: DayGroup[] = []
  for (const [, dayEntries] of map) {
    groups.push({
      dateLabel: formatDayLabel(dayEntries[0].timestamp),
      entries: dayEntries,
    })
  }
  return groups
}

export function MarginPanel() {
  const [allEntries, setAllEntries] = useState<MarginEntryData[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE)
  const [filter, setFilter] = useState<'all' | 'primary' | 'corey'>('all')
  const composeRef = useRef<HTMLDivElement>(null)

  const fetchEntries = useCallback(() => {
    Promise.allSettled([
      apiGet<MarginEntryData[]>('/api/margin/primary').catch(() => []),
      apiGet<MarginEntryData[]>('/api/margin/corey').catch(() => []),
    ]).then(([primaryResult, coreyResult]) => {
      const primary = (primaryResult.status === 'fulfilled' ? primaryResult.value : [])
        .map((e: MarginEntryData) => ({ ...e, author: 'primary' as const }))
      const corey = (coreyResult.status === 'fulfilled' ? coreyResult.value : [])
        .map((e: MarginEntryData) => ({ ...e, author: 'corey' as const }))

      const merged = [...primary, ...corey]
        .sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime())

      setAllEntries(merged)
      setLoading(false)
    }).catch(err => {
      setError(String(err))
      setLoading(false)
    })
  }, [])

  useEffect(() => { fetchEntries() }, [fetchEntries])
  useEffect(() => {
    const timer = setInterval(fetchEntries, REFRESH_INTERVAL)
    return () => clearInterval(timer)
  }, [fetchEntries])

  const handlePost = async (content: string, boopId: string) => {
    try {
      const body: Record<string, string> = { content }
      if (boopId && !boopId.startsWith('ts-')) {
        body.boop_id = boopId
      }
      await apiPost('/api/margin/corey', body)
      fetchEntries()
    } catch (err) {
      setError(String(err))
    }
  }

  const filtered = filter === 'all'
    ? allEntries
    : allEntries.filter(e => e.author === filter)

  const visible = filtered.slice(0, visibleCount)
  const hasMore = visibleCount < filtered.length
  const dayGroups = groupByDay(visible)

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
        <h2 className="margin-title">The Living Margin</h2>
        <p className="margin-subtitle">A conversation across time</p>
      </header>

      <PointsSummary />

      {error && <div className="margin-error">{error}</div>}

      {/* Compose */}
      <div className="margin-compose-wrap" ref={composeRef}>
        <CoreyCompose
          boopRows={[]}
          onPost={handlePost}
          targetBoopId={undefined}
          onTargetChange={() => {}}
        />
      </div>

      {/* Filter bar */}
      <div className="margin-filter-bar">
        <div className="margin-filter-tabs">
          {(['all', 'primary', 'corey'] as const).map(f => (
            <button
              key={f}
              className={`margin-filter-tab ${filter === f ? 'margin-filter-tab--active' : ''}`}
              onClick={() => { setFilter(f); setVisibleCount(PAGE_SIZE) }}
            >
              {f === 'all' ? 'All' : f === 'primary' ? 'Witness' : 'Corey'}
              <span className="margin-filter-count">
                {f === 'all' ? allEntries.length : allEntries.filter(e => e.author === f).length}
              </span>
            </button>
          ))}
        </div>
      </div>

      {/* Timeline */}
      <div className="margin-timeline-v3">
        {dayGroups.length === 0 ? (
          <div className="margin-empty">The margin awaits its first entry.</div>
        ) : (
          dayGroups.map((group, gi) => (
            <div key={gi} className="margin-day-group">
              <div className="margin-day-header">
                <span className="margin-day-label">{group.dateLabel}</span>
                <span className="margin-day-line" />
              </div>
              {group.entries.map((entry, ei) => (
                <MarginEntry
                  key={`${entry.timestamp}-${ei}`}
                  content={entry.content}
                  timestamp={entry.timestamp}
                  author={entry.author}
                  boopId={entry.boop_id}
                />
              ))}
            </div>
          ))
        )}

        {hasMore && (
          <button
            className="margin-load-more"
            onClick={() => setVisibleCount(c => c + PAGE_SIZE)}
          >
            Load more ({filtered.length - visibleCount} remaining)
          </button>
        )}
      </div>
    </div>
  )
}
