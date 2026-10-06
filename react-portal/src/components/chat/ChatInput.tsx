import { useState, useRef, useEffect, type FormEvent, type KeyboardEvent } from 'react'
import { useSpeechRecognition } from '../../hooks/useSpeechRecognition'
import { apiGet } from '../../api/client'
import './ChatInput.css'

interface SlashCommand {
  cmd: string
  desc: string
  type: string
}

interface ChatInputProps {
  onSend: (text: string) => void
  onUpload: (file: File) => void
  sending: boolean
}

// Plain-language text for Web Speech API failure codes (see useSpeechRecognition).
const VOICE_ERROR_TEXT: Record<string, string> = {
  'no-speech': "Didn't hear any speech. Check that the right microphone is selected in your browser/system and isn't muted.",
  'no-result': "The mic opened but no words came back. Check the selected microphone and its input level.",
  'audio-capture': 'No working microphone found. Check that a mic is connected and selected.',
  'not-allowed': 'Microphone permission is blocked for this site. Allow it via the lock icon in the address bar.',
  'service-not-allowed': "This browser's speech service is turned off or unavailable. Try Chrome or Edge.",
  'network': "Couldn't reach the browser's speech service (it runs online). Check your connection, VPN or firewall, or try Chrome.",
  'language-not-supported': 'Speech language not supported by this browser.',
  'start-failed': 'Voice input could not start. Reload the page and try again.',
}

export function ChatInput({ onSend, onUpload, sending }: ChatInputProps) {
  const [text, setText] = useState('')
  const [slashCommands, setSlashCommands] = useState<SlashCommand[]>([])
  const [showSlash, setShowSlash] = useState(false)
  const [slashIndex, setSlashIndex] = useState(0)
  const fileRef = useRef<HTMLInputElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  // Whatever the user had typed BEFORE they hit the mic. Captured once at
  // record-start; the rendered value is always base + transcript, never
  // previous-render + transcript (which would re-accumulate every event).
  const baseTextRef = useRef('')
  const { isListening, isSupported, transcript, start, stop, debugEvents, error: voiceError } = useSpeechRecognition()
  const voiceDebug = /[?&](debug|voicedebug)=1(?:&|$)/.test(window.location.search)

  // Fetch slash commands once
  useEffect(() => {
    apiGet<{ slash_commands: SlashCommand[] }>('/api/shortcuts')
      .then(data => setSlashCommands(data.slash_commands || []))
      .catch(() => {})
  }, [])

  // Sync speech transcript into text. Always render base + transcript, where
  // base = the text present when recording started. This is a REPLACE of the
  // whole value from a stable base each event — never previous-render +
  // transcript — so nothing re-accumulates and the user's pre-dictation text is
  // preserved.
  useEffect(() => {
    if (isListening && transcript) {
      const base = baseTextRef.current
      const sep = base && !/\s$/.test(base) ? ' ' : ''
      setText(base + sep + transcript)
    }
  }, [transcript, isListening])

  const filteredCommands = text.startsWith('/')
    ? slashCommands.filter(c => c.cmd.toLowerCase().startsWith(text.toLowerCase()))
    : []

  useEffect(() => {
    setShowSlash(text.startsWith('/') && filteredCommands.length > 0)
    setSlashIndex(0)
  }, [text, filteredCommands.length])

  const handleSubmit = (e?: FormEvent) => {
    e?.preventDefault()
    const trimmed = text.trim()
    if (!trimmed || sending) return
    onSend(trimmed)
    setText('')
    setShowSlash(false)
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
  }

  const selectSlashCommand = (cmd: string) => {
    setText(cmd + ' ')
    setShowSlash(false)
    textareaRef.current?.focus()
  }

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (showSlash) {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setSlashIndex(i => Math.min(i + 1, filteredCommands.length - 1))
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        setSlashIndex(i => Math.max(i - 1, 0))
        return
      }
      if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
        e.preventDefault()
        if (filteredCommands[slashIndex]) {
          selectSlashCommand(filteredCommands[slashIndex].cmd)
        }
        return
      }
      if (e.key === 'Escape') {
        setShowSlash(false)
        return
      }
    }

    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSubmit()
    }
  }

  const handleInput = () => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 150) + 'px'
  }

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) {
      onUpload(file)
      e.target.value = ''
    }
  }

  const toggleMic = () => {
    if (isListening) {
      stop()
    } else {
      // Capture the pre-dictation text ONCE, at record-start, so the sync effect
      // can always render base + transcript.
      baseTextRef.current = text
      start()
    }
  }

  return (
    <div className="chat-input-container">
      {voiceError && !isListening && (
        <div className="chat-voice-error" role="status">
          {'\u{1F3A4}'} {VOICE_ERROR_TEXT[voiceError] || `Voice input stopped (${voiceError}).`}
        </div>
      )}
      {voiceDebug && (
        <div
          className="voice-debug-panel"
          style={{
            fontFamily: 'monospace',
            fontSize: '11px',
            lineHeight: 1.35,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            background: '#101418',
            color: '#8be28b',
            border: '1px solid #2a3138',
            borderRadius: '6px',
            padding: '8px 10px',
            margin: '0 0 8px 0',
            maxHeight: '180px',
            overflowY: 'auto',
          }}
        >
          <div style={{ color: '#e2c14c', marginBottom: '4px' }}>
            VOICE DEBUG (raw event.results, last {debugEvents.length} events) — listening={String(isListening)} error={String(voiceError)}
          </div>
          {debugEvents.length === 0 && <div>(no events yet — tap the mic and speak)</div>}
          {debugEvents.map((snap, ei) => (
            <div key={ei} style={{ borderTop: '1px dashed #2a3138', paddingTop: '3px', marginTop: '3px' }}>
              <div style={{ color: '#6fa8dc' }}>--- event {ei + 1} ---</div>
              {snap.map((r, ri) => (
                <div key={ri}>[{r.index}] isFinal={String(r.isFinal)} "{r.transcript}"</div>
              ))}
            </div>
          ))}
        </div>
      )}
      <form className="chat-input-form" onSubmit={handleSubmit}>
        <button
          type="button"
          className="chat-upload-btn"
          onClick={() => fileRef.current?.click()}
          title="Upload file"
        >
          +
        </button>
        <input
          type="file"
          ref={fileRef}
          className="sr-only"
          onChange={handleFileChange}
        />
        <div className="chat-textarea-wrap">
          {showSlash && (
            <div className="slash-dropdown">
              {filteredCommands.map((cmd, i) => (
                <button
                  key={cmd.cmd}
                  type="button"
                  className={`slash-item ${i === slashIndex ? 'slash-item-active' : ''}`}
                  onClick={() => selectSlashCommand(cmd.cmd)}
                  onMouseEnter={() => setSlashIndex(i)}
                >
                  <span className="slash-cmd">{cmd.cmd}</span>
                  <span className="slash-desc">{cmd.desc}</span>
                </button>
              ))}
            </div>
          )}
          <textarea
            ref={textareaRef}
            className="chat-textarea"
            placeholder="Type a message... (/ for commands)"
            value={text}
            onChange={e => setText(e.target.value)}
            onKeyDown={handleKeyDown}
            onInput={handleInput}
            rows={1}
            disabled={sending}
          />
        </div>
        {isSupported && (
          <button
            type="button"
            className={`chat-mic-btn ${isListening ? 'chat-mic-active' : ''}`}
            onClick={toggleMic}
            title={isListening ? 'Stop recording' : 'Voice input'}
          >
            {'\u{1F3A4}'}
          </button>
        )}
        <button
          type="submit"
          className="chat-send-btn"
          disabled={!text.trim() || sending}
        >
          {sending ? '...' : '\u{27A4}'}
        </button>
      </form>
    </div>
  )
}
