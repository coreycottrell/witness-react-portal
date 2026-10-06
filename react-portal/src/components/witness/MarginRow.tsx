/**
 * MarginRow — single BOOP row with left (Witness) and right (Corey) entry.
 */
import { MarginEntry } from './MarginEntry'
import { MarginEmptySlot } from './MarginEmptySlot'

export interface MarginEntryData {
  timestamp: string
  author: 'primary' | 'corey'
  content: string
  boop_id?: string
}

export interface BoopRow {
  boop_id: string
  witnessEntries: MarginEntryData[]
  coreyEntries: MarginEntryData[]
  timestamp: string
}

interface MarginRowProps {
  row: BoopRow
  onReply?: (boopId: string) => void
}

function formatBoopTime(ts: string): string {
  try {
    const d = new Date(ts)
    return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  } catch {
    return ''
  }
}

export function MarginRow({ row, onReply }: MarginRowProps) {
  const timeStr = formatBoopTime(row.timestamp)
  const boopLabel = row.boop_id.startsWith('ts-')
    ? timeStr
    : `BOOP #${row.boop_id}`

  return (
    <div className="margin-row">
      <div className="margin-row__header">
        <span className="margin-row__boop-label">{boopLabel}</span>
        {timeStr && !row.boop_id.startsWith('ts-') && (
          <span className="margin-row__time">{timeStr}</span>
        )}
      </div>
      <div className="margin-row__columns">
        <div className="margin-row__col margin-row__col--witness">
          {row.witnessEntries.length > 0 ? (
            row.witnessEntries.map((e, i) => (
              <MarginEntry
                key={i}
                content={e.content}
                timestamp={e.timestamp}
                author="primary"
              />
            ))
          ) : (
            <MarginEmptySlot side="witness" />
          )}
        </div>
        <div className="margin-row__col margin-row__col--corey">
          {row.coreyEntries.length > 0 ? (
            row.coreyEntries.map((e, i) => (
              <MarginEntry
                key={i}
                content={e.content}
                timestamp={e.timestamp}
                author="corey"
              />
            ))
          ) : (
            <MarginEmptySlot
              side="corey"
              onReply={onReply ? () => onReply(row.boop_id) : undefined}
            />
          )}
        </div>
      </div>
    </div>
  )
}
