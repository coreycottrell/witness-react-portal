import { apiGet, apiPost } from './client'
import type { ChatMessage, ReactionRequest } from '../types/chat'

export function fetchChatHistory(last = 100): Promise<{ messages: ChatMessage[] }> {
  return apiGet<{ messages: ChatMessage[] }>(`/api/chat/history?last=${last}`)
}

export function sendChatMessage(message: string, sender?: string): Promise<{ ok: boolean }> {
  // P20: include the self-selected operator so Primary sees WHO is speaking.
  return apiPost<{ ok: boolean }>('/api/chat/send', sender ? { message, sender } : { message })
}

export function sendReaction(req: ReactionRequest): Promise<{ ok: boolean; sentiment: string }> {
  return apiPost<{ ok: boolean; sentiment: string }>('/api/reaction', req)
}
