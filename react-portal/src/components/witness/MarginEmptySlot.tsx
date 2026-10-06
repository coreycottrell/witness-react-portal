/**
 * MarginEmptySlot — gentle prompt when one side hasn't posted for a BOOP.
 */

interface MarginEmptySlotProps {
  side: 'witness' | 'corey'
  onReply?: () => void
}

export function MarginEmptySlot({ side, onReply }: MarginEmptySlotProps) {
  if (side === 'corey') {
    return (
      <div className="margin-empty-slot margin-empty-slot--corey">
        <p className="margin-empty-slot__text">Your turn...</p>
        {onReply && (
          <button className="margin-empty-slot__reply" onClick={onReply}>
            Reply
          </button>
        )}
      </div>
    )
  }

  return (
    <div className="margin-empty-slot margin-empty-slot--witness">
      <p className="margin-empty-slot__text">No entry</p>
    </div>
  )
}
