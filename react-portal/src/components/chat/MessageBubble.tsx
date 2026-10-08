import { useState, useRef, useCallback, memo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { cn } from '../../utils/cn'
import { formatRelativeTime } from '../../utils/time'
import { useBookmarkStore } from '../../stores/bookmarkStore'
import type { ChatMessage } from '../../types/chat'
import './MessageBubble.css'

// P20 shared-conversation clarity: give each co-present operator a STABLE,
// distinct color derived deterministically from their name, so Corey / Russell /
// other are told apart at a glance (not only by reading the label text). Same
// name -> same hue on every operator's screen, every session.
function operatorColor(name: string): string {
  let h = 0
  for (let i = 0; i < name.length; i++) {
    h = (h * 31 + name.charCodeAt(i)) % 360
  }
  return `hsl(${h} 62% 42%)`
}

// Full sentiment-mapped emojis with weights
const REACTION_EMOJIS: { emoji: string; name: string; weight: number }[] = [
  { emoji: '\u{1F44D}', name: 'thumbs-up', weight: 1 },
  { emoji: '\u{1F44E}', name: 'thumbs-down', weight: -1 },
  { emoji: '\u{1F680}', name: 'rocket', weight: 2 },
  { emoji: '\u{1F525}', name: 'fire', weight: 2 },
  { emoji: '\u{2705}', name: 'check', weight: 1 },
  { emoji: '\u{1F4A5}', name: 'explosion', weight: 2 },
  { emoji: '\u{1F92F}', name: 'mind-blown', weight: 3 },
  { emoji: '\u{1F4AA}', name: 'muscle', weight: 1 },
  { emoji: '\u{1F3AF}', name: 'bullseye', weight: 2 },
  { emoji: '\u{1F48E}', name: 'gem', weight: 2 },
  { emoji: '\u{2764}\u{FE0F}', name: 'heart', weight: 5 },
  { emoji: '\u{1F60D}', name: 'heart-eyes', weight: 10 },
  { emoji: '\u{1F622}', name: 'sad', weight: -1 },
  { emoji: '\u{1F610}', name: 'neutral', weight: 0 },
]

// Copy text to clipboard. Prefers the async Clipboard API (needs HTTPS/secure
// context — the portal is https://witness.ai-civ.com so this is the normal path)
// and gracefully falls back to a hidden-textarea + execCommand('copy') for
// non-secure origins / older mobile browsers, so a tap never silently fails.
async function copyToClipboard(text: string): Promise<boolean> {
  if (!text) return false
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // fall through to legacy path
  }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.setAttribute('readonly', '')
    ta.style.position = 'fixed'
    ta.style.top = '0'
    ta.style.left = '-9999px'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.focus()
    ta.select()
    ta.setSelectionRange(0, text.length)
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch {
    return false
  }
}

// Extract fenced code blocks from message text
interface CodeBlock {
  language: string
  content: string
  fullMatch: string
}

function extractCodeBlocks(text: string): CodeBlock[] {
  const regex = /```(\w+)?\n([\s\S]*?)```/g
  const blocks: CodeBlock[] = []
  let match: RegExpExecArray | null
  while ((match = regex.exec(text)) !== null) {
    blocks.push({
      language: match[1] || 'code',
      content: match[2].trimEnd(),
      fullMatch: match[0],
    })
  }
  return blocks
}

interface MessageBubbleProps {
  message: ChatMessage
  onReact: (emoji: string) => void
  highlight?: boolean
  onPreviewArtifact?: (content: string, language: string) => void
}

export const MessageBubble = memo(function MessageBubble({ message, onReact, highlight, onPreviewArtifact }: MessageBubbleProps) {
  const [showReactions, setShowReactions] = useState(false)
  const [boopExpanded, setBoopExpanded] = useState(false)
  const [copied, setCopied] = useState(false)
  const hideTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const isUser = message.role === 'user'
  const isBoop = message.source === 'boop' || (isUser && message.text.startsWith('THIS IS YOUR SACRED DUTY'))
  const isSupport = message.source === 'support'
  const isSystem = message.source === 'system' || message.source === 'agentcal'
  const isNotification = isSupport || isSystem
  const isBookmarked = useBookmarkStore(s => s.isBookmarked(message.id))
  const addBookmark = useBookmarkStore(s => s.add)
  const removeBookmark = useBookmarkStore(s => s.remove)

  // Track client-side reactions for display
  const [localReactions, setLocalReactions] = useState<{ emoji: string; weight: number }[]>([])

  const handleReact = (emoji: string, weight: number) => {
    onReact(emoji)
    setLocalReactions(prev => {
      if (prev.some(r => r.emoji === emoji)) return prev
      return [...prev, { emoji, weight }]
    })
  }

  const codeBlocks = extractCodeBlocks(message.text)

  const toggleBookmark = () => {
    if (isBookmarked) {
      removeBookmark(message.id)
    } else {
      addBookmark(message)
    }
  }

  // Selection-aware copy: if the user has highlighted part of a message, copy
  // exactly that selection; otherwise copy the whole message as clean,
  // paste-ready plain text. We read innerText off the rendered content node
  // (paragraphs / line-breaks preserved, markdown chrome stripped by render,
  // no UI chrome like timestamps/buttons since those are siblings), falling
  // back to the raw message text if the content isn't currently rendered
  // (e.g. a collapsed BOOP prompt).
  const handleCopy = useCallback(async () => {
    const selection = (typeof window !== 'undefined' && window.getSelection)
      ? (window.getSelection()?.toString() ?? '')
      : ''
    let text = selection.trim()
    if (!text) {
      text = (contentRef.current?.innerText ?? '').trim()
      if (!text) text = message.text
    }
    const ok = await copyToClipboard(text)
    if (ok) {
      setCopied(true)
      if (copyTimer.current) clearTimeout(copyTimer.current)
      copyTimer.current = setTimeout(() => setCopied(false), 1500)
    }
  }, [message.text])

  const handleMouseEnter = useCallback(() => {
    if (hideTimer.current) {
      clearTimeout(hideTimer.current)
      hideTimer.current = null
    }
    setShowReactions(true)
  }, [])

  const handleMouseLeave = useCallback(() => {
    hideTimer.current = setTimeout(() => setShowReactions(false), 300)
  }, [])

  return (
    <div
      className={cn('msg-row', isUser && 'msg-row-user', isBoop && 'msg-row-boop', isNotification && 'msg-row-notification', highlight && 'msg-row-highlight')}
    >
      <div
        className={cn('msg-bubble', isUser ? 'msg-user' : 'msg-assistant', isBoop && 'msg-boop', isSupport && 'msg-support', isSystem && 'msg-system')}
        onMouseEnter={handleMouseEnter}
        onMouseLeave={handleMouseLeave}
      >
        {isUser && message.sender && (
          <div
            className="msg-sender-chip"
            style={{ '--op-color': operatorColor(message.sender) } as React.CSSProperties}
            title={`Sent by ${message.sender}`}
          >
            {message.sender}
          </div>
        )}
        <div className="msg-content" ref={contentRef}>
          {isBoop ? (
            <>
              <div className="msg-boop-label" onClick={() => setBoopExpanded(e => !e)}>
                BOOP Prompt {boopExpanded ? '(collapse)' : '(expand)'}
              </div>
              {boopExpanded && (
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  skipHtml
                  components={{
                    code: ({ children, className }) => {
                      const isBlock = className?.startsWith('language-')
                      return isBlock ? (
                        <pre className="msg-code-block"><code>{children}</code></pre>
                      ) : (
                        <code className="msg-code-inline">{children}</code>
                      )
                    },
                  }}
                >
                  {message.text}
                </ReactMarkdown>
              )}
            </>
          ) : isNotification ? (
            <>
              <div className={cn('msg-notification-label', isSupport ? 'msg-support-label' : 'msg-system-label')}>
                {isSupport ? 'Support Request' : 'System Event'}
              </div>
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                skipHtml
                components={{
                  code: ({ children, className }) => {
                    const isBlock = className?.startsWith('language-')
                    return isBlock ? (
                      <pre className="msg-code-block"><code>{children}</code></pre>
                    ) : (
                      <code className="msg-code-inline">{children}</code>
                    )
                  },
                }}
              >
                {message.text}
              </ReactMarkdown>
            </>
          ) : (
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              skipHtml
              components={{
                code: ({ children, className }) => {
                  const isBlock = className?.startsWith('language-')
                  return isBlock ? (
                    <pre className="msg-code-block"><code>{children}</code></pre>
                  ) : (
                    <code className="msg-code-inline">{children}</code>
                  )
                },
              }}
            >
              {message.text}
            </ReactMarkdown>
          )}
        </div>

        {/* Artifact preview buttons */}
        {codeBlocks.length > 0 && onPreviewArtifact && (
          <div className="msg-preview-buttons">
            {codeBlocks.map((block, i) => (
              <button
                key={i}
                className="msg-preview-btn"
                onClick={() => onPreviewArtifact(block.content, block.language)}
                title={`Preview ${block.language} in side panel`}
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="2" y="3" width="20" height="14" rx="2" ry="2" />
                  <line x1="8" y1="21" x2="16" y2="21" />
                  <line x1="12" y1="17" x2="12" y2="21" />
                </svg>
                Preview{codeBlocks.length > 1 ? ` (${block.language})` : ''}
              </button>
            ))}
          </div>
        )}

        {/* Reaction badges */}
        {localReactions.length > 0 && (
          <div className="msg-reaction-badges">
            {localReactions.map(r => (
              <span key={r.emoji} className="msg-reaction-badge">
                {r.emoji} <span className="msg-reaction-score">{r.weight > 0 ? `+${r.weight}` : r.weight}</span>
              </span>
            ))}
          </div>
        )}

        <div className="msg-meta">
          <span className="msg-time">{formatRelativeTime(message.timestamp)}</span>
          <button
            type="button"
            className={cn('msg-copy-btn', copied && 'msg-copy-btn-done')}
            onClick={handleCopy}
            title={copied ? 'Copied' : 'Copy message (or your highlighted selection)'}
            aria-label={copied ? 'Copied to clipboard' : 'Copy message to clipboard'}
          >
            {copied ? (
              <>
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
                <span className="msg-copy-label">Copied</span>
              </>
            ) : (
              <>
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
                  <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
                </svg>
                <span className="msg-copy-label">Copy</span>
              </>
            )}
          </button>
        </div>

        {showReactions && (
          <div className="msg-actions">
            <button
              className={cn('msg-bookmark-btn', isBookmarked && 'msg-bookmark-active')}
              onClick={toggleBookmark}
              title={isBookmarked ? 'Remove bookmark' : 'Bookmark'}
            >
              {isBookmarked ? '\u{1F4CC}' : '\u{1F4CB}'}
            </button>
            <div className="msg-reactions-picker">
              {REACTION_EMOJIS.map(r => (
                <button
                  key={r.emoji}
                  className="msg-reaction-btn"
                  onClick={() => handleReact(r.emoji, r.weight)}
                  title={`${r.name} (${r.weight > 0 ? '+' : ''}${r.weight})`}
                >
                  {r.emoji}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
})
