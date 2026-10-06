import { describe, it, expect, vi, beforeEach } from 'vitest'

// Capture the ws handler chatStore registers so the test can fire a server echo.
let wsHandler: ((m: any) => void) | null = null
vi.mock('../api/websocket', () => ({
  chatWs: {
    connect: vi.fn(),
    disconnect: vi.fn(),
    onMessage: (h: (m: any) => void) => { wsHandler = h; return () => { wsHandler = null } },
  },
}))

let resolveSend: (() => void) | null = null
vi.mock('../api/chat', () => ({
  fetchChatHistory: vi.fn(),
  sendReaction: vi.fn(),
  sendChatMessage: vi.fn(() => new Promise<{ ok: boolean }>(res => { resolveSend = () => res({ ok: true }) })),
}))

import { useChatStore } from '../stores/chatStore'

beforeEach(() => {
  useChatStore.setState({ messages: [], sending: false })
})

describe('chatStore send vs ws echo (duplicate-message race)', () => {
  it('ws echo arrives BEFORE the POST resolves -> still exactly one row', async () => {
    useChatStore.getState().connectWs()
    const p = useChatStore.getState().send('hello world', 'Russell')
    // server broadcast reaches the browser first
    wsHandler!({ id: 'portal-1791206791429', role: 'user', text: 'hello world', timestamp: 1791206791, sender: 'Russell' })
    resolveSend!()
    await p
    const rows = useChatStore.getState().messages.filter(m => m.role === 'user' && m.text === 'hello world')
    expect(rows).toHaveLength(1)
    expect(rows[0].id).toBe('portal-1791206791429')
  })

  it('POST resolves BEFORE the ws echo -> local row replaced, one row', async () => {
    useChatStore.getState().connectWs()
    const p = useChatStore.getState().send('second one', 'Russell')
    resolveSend!()
    await p
    wsHandler!({ id: 'portal-2', role: 'user', text: 'second one', timestamp: 1, sender: 'Russell' })
    const rows = useChatStore.getState().messages.filter(m => m.text === 'second one')
    expect(rows).toHaveLength(1)
    expect(rows[0].id).toBe('portal-2')
  })

  it('same text sent twice on purpose still shows two rows', async () => {
    useChatStore.getState().connectWs()
    let p = useChatStore.getState().send('ok', 'Russell')
    wsHandler!({ id: 'portal-a', role: 'user', text: 'ok', timestamp: 1 })
    resolveSend!(); await p
    p = useChatStore.getState().send('ok', 'Russell')
    wsHandler!({ id: 'portal-b', role: 'user', text: 'ok', timestamp: 2 })
    resolveSend!(); await p
    expect(useChatStore.getState().messages.filter(m => m.text === 'ok')).toHaveLength(2)
  })
})
