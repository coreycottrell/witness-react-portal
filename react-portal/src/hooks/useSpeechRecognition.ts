import { useState, useRef, useCallback } from 'react'

// One raw recognition result, as observed in a single onresult event.
export interface RawSpeechResult {
  index: number
  isFinal: boolean
  transcript: string
}

interface SpeechRecognitionHook {
  isListening: boolean
  isSupported: boolean
  transcript: string
  start: () => void
  stop: () => void
  // Debug net: the raw event.results of the last ~5 onresult events. Only
  // populated when ?debug=1 / ?voicedebug=1 is present in the URL. Rendered by
  // ChatInput's on-screen voice-debug-panel so the operator can screenshot the
  // real device stream if a fix ever fails again.
  debugEvents: RawSpeechResult[][]
  // Why the last listening session ended without text, if it did. The Web Speech
  // API reports failures ONLY via onerror(event.error) — previously swallowed, so
  // a desktop mic that lit up but produced nothing failed SILENTLY. Values are the
  // spec codes ('no-speech' | 'audio-capture' | 'not-allowed' | 'network' |
  // 'service-not-allowed' | 'language-not-supported' | 'aborted') plus our own
  // 'no-result' (session ended with zero results and no error) and 'start-failed'.
  error: string | null
}

// Merge a newly-seen hypothesis into an accumulator, COLLAPSING prefix-supersets.
// This is the heart of the mobile fix. Mobile speech engines (Android Chrome,
// continuous=true) re-emit the CUMULATIVE (growing superset) phrase at a NEW
// results index each event, each marked isFinal — so the old index-keyed JOIN
// concatenated every cumulative snapshot ("this" + "this is" + "this is the"...).
// Here, if `next` extends `acc` (mobile cumulative), `next` REPLACES `acc`; if
// `acc` already contains `next` (a stale/short snapshot), we keep `acc`; only a
// GENUINELY new segment (neither is a prefix of the other, e.g. desktop's
// distinct multi-segment lists) is appended. Result: mobile collapses to the one
// longest hypothesis — exactly like the clean single-index laptop case.
// Normalize a hypothesis for the prefix/superset COMPARISON ONLY — never for the
// returned/displayed/sent value. Mobile engines re-emit the cumulative hypothesis
// with inserted capitalization and punctuation ("good morning witness" ->
// "Good morning, Witness. How's...") so a later snapshot no longer LITERALLY
// .startsWith() the earlier one; it would then wrongly fall to the APPEND branch
// and STACK. Lowercasing + stripping punctuation + collapsing whitespace makes the
// prefix test case/punctuation-insensitive. The ORIGINAL-cased/punctuated longest
// hypothesis is still what we return (and therefore display AND send).
function normalizeForCompare(s: string): string {
  return s.toLowerCase().replace(/[^\w\s]/g, '').replace(/\s+/g, ' ').trim()
}

function mergeHypothesis(acc: string, next: string): string {
  if (!next) return acc
  if (!acc) return next
  const a = normalizeForCompare(acc)
  const n = normalizeForCompare(next)
  if (!n) return acc                      // next was punctuation-only -> keep acc
  if (!a) return next                     // acc was punctuation-only -> take next
  if (n.startsWith(a)) return next        // superset -> replace (mobile cumulative)
  if (a.startsWith(n)) return acc         // subset/duplicate -> keep the longer
  const sep = /\s$/.test(acc) || /^\s/.test(next) ? '' : ' '
  return acc + sep + next                 // distinct segment -> append
}

const VOICE_DEBUG = typeof window !== 'undefined'
  && /[?&](debug|voicedebug)=1(?:&|$)/.test(window.location.search)

// Web Speech API types
interface SpeechRecognitionEvent {
  results: SpeechRecognitionResultList
  resultIndex: number
}

interface SpeechRecognitionResultList {
  length: number
  item(index: number): SpeechRecognitionResult
  [index: number]: SpeechRecognitionResult
}

interface SpeechRecognitionResult {
  isFinal: boolean
  length: number
  item(index: number): SpeechRecognitionAlternative
  [index: number]: SpeechRecognitionAlternative
}

interface SpeechRecognitionAlternative {
  transcript: string
  confidence: number
}

interface SpeechRecognitionInstance {
  continuous: boolean
  interimResults: boolean
  lang: string
  start(): void
  stop(): void
  abort(): void
  onresult: ((event: SpeechRecognitionEvent) => void) | null
  onerror: ((event: { error: string; message?: string }) => void) | null
  onend: (() => void) | null
}

declare global {
  interface Window {
    SpeechRecognition?: new () => SpeechRecognitionInstance
    webkitSpeechRecognition?: new () => SpeechRecognitionInstance
  }
}

function getSpeechRecognition(): (new () => SpeechRecognitionInstance) | null {
  return window.SpeechRecognition || window.webkitSpeechRecognition || null
}

export function useSpeechRecognition(): SpeechRecognitionHook {
  const [isListening, setIsListening] = useState(false)
  const [transcript, setTranscript] = useState('')
  const [debugEvents, setDebugEvents] = useState<RawSpeechResult[][]>([])
  const [error, setError] = useState<string | null>(null)
  const gotResultRef = useRef(false)
  const erroredRef = useRef(false)
  const recognitionRef = useRef<SpeechRecognitionInstance | null>(null)

  const isSupported = getSpeechRecognition() !== null

  const start = useCallback(() => {
    const SpeechRecognitionClass = getSpeechRecognition()
    if (!SpeechRecognitionClass) return

    if (VOICE_DEBUG) setDebugEvents([])
    setError(null)
    gotResultRef.current = false
    erroredRef.current = false
    const recognition = new SpeechRecognitionClass()
    recognition.continuous = true
    recognition.interimResults = true
    recognition.lang = 'en-US'

    recognition.onresult = (event: SpeechRecognitionEvent) => {
      gotResultRef.current = true
      // Recompute the WHOLE transcript from the authoritative results list every
      // event — never previous-value += this-event. Mobile re-emits the growing
      // CUMULATIVE phrase at a NEW index each event, each marked isFinal; the old
      // index-keyed join concatenated those cumulative snapshots and produced the
      // garble ("this" + "this is" + "this is the"...). mergeHypothesis collapses
      // prefix-supersets so a longer hypothesis REPLACES the shorter one it
      // extends, making mobile behave exactly like the clean single-index laptop
      // case. Finals feed `committed` in index order; the live interim is merged
      // last (it too may be a superset of the committed finals).
      let committed = ''
      let interim = ''
      const snapshot: RawSpeechResult[] = []
      for (let i = 0; i < event.results.length; i++) {
        const result = event.results[i]
        const text = result[0].transcript
        if (VOICE_DEBUG) {
          snapshot.push({ index: i, isFinal: result.isFinal, transcript: text })
        }
        if (result.isFinal) {
          committed = mergeHypothesis(committed, text)
        } else {
          interim = mergeHypothesis(interim, text)
        }
      }
      const full = mergeHypothesis(committed, interim)
      setTranscript(full)
      if (VOICE_DEBUG) {
        setDebugEvents(prev => [...prev.slice(-4), snapshot])
      }
    }

    recognition.onerror = (event) => {
      erroredRef.current = true
      // 'aborted' is our own stop()/abort — not a user-facing failure.
      if (event && event.error && event.error !== 'aborted') {
        setError(event.error)
        console.warn('[voice] SpeechRecognition error:', event.error, event.message || '')
      }
      setIsListening(false)
    }

    recognition.onend = () => {
      if (!gotResultRef.current && !erroredRef.current) setError('no-result')
      setIsListening(false)
    }

    recognitionRef.current = recognition
    try {
      recognition.start()
    } catch (e) {
      console.warn('[voice] recognition.start() threw:', e)
      recognitionRef.current = null
      setError('start-failed')
      setIsListening(false)
      return
    }
    setIsListening(true)
    setTranscript('')
  }, [])

  const stop = useCallback(() => {
    if (recognitionRef.current) {
      recognitionRef.current.stop()
      recognitionRef.current = null
    }
    setIsListening(false)
  }, [])

  return { isListening, isSupported, transcript, start, stop, debugEvents, error }
}
