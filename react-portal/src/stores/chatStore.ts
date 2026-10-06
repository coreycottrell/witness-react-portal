import { create } from 'zustand'
import { fetchChatHistory, sendChatMessage, sendReaction } from '../api/chat'
import { chatWs } from '../api/websocket'
import type { ChatMessage } from '../types/chat'

let wsCleanup: (() => void) | null = null

interface ChatState {
  messages: ChatMessage[]
  loading: boolean
  sending: boolean
  wsConnected: boolean
  error: string | null
  loadHistory: () => Promise<void>
  send: (text: string, sender?: string) => Promise<void>
  react: (msgId: string, emoji: string, msgText: string, msgRole: 'user' | 'assistant') => Promise<void>
  connectWs: () => void
  disconnectWs: () => void
}

export const useChatStore = create<ChatState>((set, get) => ({
  messages: [],
  loading: false,
  sending: false,
  wsConnected: false,
  error: null,

  loadHistory: async () => {
    set({ loading: true, error: null })
    try {
      const data = await fetchChatHistory(200)
      const msgs = data.messages || []
      set({ messages: msgs, loading: false })
    } catch (e) {
      set({ loading: false, error: e instanceof Error ? e.message : 'Failed to load chat' })
    }
  },

  send: async (text: string, sender?: string) => {
    set({ sending: true })
    // Duplicate-message fix (2026-10-05): the server's /ws/chat broadcast can reach
    // the browser BEFORE the POST response resolves. The old code then appended a
    // local- echo on top of the already-delivered server row (two rows, one send).
    // Snapshot the ids present before the POST; if a NEW non-local user row with the
    // same text appeared while we awaited, that IS the server echo - skip the local row.
    // (Id-snapshot, not timestamps: browser/server clocks may disagree.)
    const idsBefore = new Set(get().messages.map(m => m.id))
    try {
      await sendChatMessage(text, sender)
      const userMsg: ChatMessage = {
        id: `local-${Date.now()}`,
        text,
        role: 'user',
        timestamp: Date.now() / 1000,
        sender,
      }
      set(s => {
        const echoed = s.messages.some(
          m => !idsBefore.has(m.id) && !m.id.startsWith('local-') &&
               m.role === 'user' && (m.text || '').trim() === text.trim()
        )
        return echoed ? { sending: false } : { messages: [...s.messages, userMsg], sending: false }
      })
    } catch (e) {
      console.error('[chat] send failed:', e)
      set({ sending: false })
    }
  },

  react: async (msgId: string, emoji: string, msgText: string, msgRole: 'user' | 'assistant') => {
    try {
      await sendReaction({
        msg_id: msgId,
        emoji,
        action: 'add',
        msg_preview: msgText.slice(0, 200),
        msg_role: msgRole,
      })
    } catch (e) {
      console.error('[chat] reaction failed:', e)
    }
  },

  connectWs: () => {
    if (wsCleanup) {
      wsCleanup()
      wsCleanup = null
    }

    chatWs.connect()
    set({ wsConnected: true })

    wsCleanup = chatWs.onMessage((msg) => {
      set((s) => {
        // Check if message already exists by ID
        const idx = s.messages.findIndex(m => m.id === msg.id)
        if (idx >= 0) {
          const updated = [...s.messages]
          updated[idx] = msg
          return { messages: updated }
        }

        // Check if this is a server echo of an optimistic local message:
        // same role + same text content → replace the local one
        if (msg.role === 'user') {
          const localIdx = s.messages.findIndex(
            m => m.id.startsWith('local-') && m.role === 'user' && m.text === msg.text
          )
          if (localIdx >= 0) {
            const updated = [...s.messages]
            updated[localIdx] = msg
            return { messages: updated }
          }
        }

        return { messages: [...s.messages, msg] }
      })
    })
  },

  disconnectWs: () => {
    if (wsCleanup) {
      wsCleanup()
      wsCleanup = null
    }
    chatWs.disconnect()
    set({ wsConnected: false })
  },
}))
