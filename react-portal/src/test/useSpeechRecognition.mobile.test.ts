import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useSpeechRecognition } from '../hooks/useSpeechRecognition'
import { legacyUseSpeechRecognition } from './fixtures/legacyUseSpeechRecognition'

/**
 * Mobile dictation regression test (portal task witness-portal-mobile-mic-20261006).
 *
 * Feeds sequences of Web Speech API onresult events (interim + final) through the REAL hooks
 * via a fake SpeechRecognition, and asserts the transcript the textbox would show.
 *
 * NOTE ON PROVENANCE: the sequences are RECONSTRUCTED from the symptom strings Russell's phone
 * produced ("clearlyclearly theclearly the microphone...") and the documented mobile behaviour
 * (portal-voice-fix-20260907/01-fix.md). They are NOT a raw device capture. The portal ships a
 * ?voicedebug=1 panel to capture a real one if this ever fails again.
 */

type R = { t: string; f: boolean }
// event.results as the browser presents it: array-like of results, each [alt0] with isFinal.
function results(list: R[]) {
  return list.map(r => Object.assign([{ transcript: r.t, confidence: 0.9 }], { isFinal: r.f, length: 1 }))
}

class FakeRecognition {
  static last: FakeRecognition | null = null
  continuous = false
  interimResults = false
  lang = ''
  onresult: ((e: any) => void) | null = null
  onerror: ((e: any) => void) | null = null
  onend: (() => void) | null = null
  constructor() { FakeRecognition.last = this }
  start() {}
  stop() {}
  abort() {}
  emit(list: R[]) { this.onresult?.({ results: results(list), resultIndex: 0 }) }
}

beforeEach(() => {
  ;(window as any).SpeechRecognition = FakeRecognition
  FakeRecognition.last = null
})
afterEach(() => { delete (window as any).SpeechRecognition })

function run(hookFn: typeof useSpeechRecognition, events: R[][]) {
  const { result } = renderHook(() => hookFn())
  act(() => { result.current.start() })
  const seen: string[] = []
  for (const ev of events) {
    act(() => { FakeRecognition.last!.emit(ev) })
    seen.push(result.current.transcript)
  }
  return { final: result.current.transcript, seen }
}

const PHRASE = 'clearly the microphone on mobile is still not working'
const words = PHRASE.split(' ')
const growing = words.map((_, i) => words.slice(0, i + 1).join(' '))

// Android Chrome, continuous=true: every partial arrives as a NEW results[] entry, still isFinal=false.
const seqNewIndexInterim: R[][] = growing.map((_, i) => growing.slice(0, i + 1).map(t => ({ t, f: false })))
// Same, but each cumulative snapshot is marked isFinal (documented v4 mobile shape).
const seqNewIndexFinal: R[][] = growing.map((_, i) => growing.slice(0, i + 1).map(t => ({ t, f: true })))
// Snapshots re-emitted with revised capitalisation/punctuation.
const seqRevised: R[][] = [
  [{ t: 'good morning witness', f: false }],
  [{ t: 'good morning witness', f: true }, { t: 'Good morning, Witness. How', f: false }],
  [{ t: 'good morning witness', f: true }, { t: 'Good morning, Witness. How', f: true }, { t: "Good morning, Witness. How's it going", f: false }],
]
// Desktop shape: ONE slot updated in place; then a second, distinct utterance appended.
const seqDesktop: R[][] = [
  [{ t: 'hello', f: false }],
  [{ t: 'hello there', f: false }],
  [{ t: 'hello there', f: true }],
  [{ t: 'hello there', f: true }, { t: ' how are', f: false }],
  [{ t: 'hello there', f: true }, { t: ' how are you', f: true }],
]

describe('FIXED hook: mobile dictation yields one clean sentence', () => {
  it('Android: each partial at a new index, isFinal=false', () => {
    const { final, seen } = run(useSpeechRecognition, seqNewIndexInterim)
    expect(final).toBe(PHRASE)
    seen.forEach((s, i) => expect(s).toBe(growing[i])) // every intermediate state is also clean
  })
  it('Android: each cumulative partial at a new index, isFinal=true', () => {
    expect(run(useSpeechRecognition, seqNewIndexFinal).final).toBe(PHRASE)
  })
  it('revised capitalisation/punctuation does not stack', () => {
    expect(run(useSpeechRecognition, seqRevised).final).toBe("Good morning, Witness. How's it going")
  })
  it('desktop single-slot + distinct second segment still appends', () => {
    expect(run(useSpeechRecognition, seqDesktop).final).toBe('hello there how are you')
  })
})

describe('NEGATIVE CONTROL: pre-fix hook reproduces the duplication Russell saw', () => {
  it('old hook garbles the Android sequence', () => {
    const { final } = run(legacyUseSpeechRecognition as any, seqNewIndexInterim)
    expect(final).not.toBe(PHRASE)
    expect(final.startsWith('clearlyclearly the')).toBe(true) // the exact symptom
    expect(final.length).toBeGreaterThan(PHRASE.length * 3)
  })
  it('old hook garbles the isFinal-cumulative sequence', () => {
    expect(run(legacyUseSpeechRecognition as any, seqNewIndexFinal).final).not.toBe(PHRASE)
  })
  it('old hook is fine on desktop shape (why laptops never showed it)', () => {
    expect(run(legacyUseSpeechRecognition as any, seqDesktop).final).toBe('hello there how are you')
  })
})

// ---------------------------------------------------------------------------------------------
// REAL-DEVICE PATTERN (ticket 3499, 2026-10-06 13:37Z). This is Russell's actual message text as
// stored in portal-chat.jsonl (id portal-1791293830176), split at each "okay" start. The old
// hook produced that text by plain-concatenating a results list that held THESE 31 entries
// (note several partials repeat 2-5 times, and the first 4 are bare "okay"). Fed to the fixed
// hook, the same event must collapse to the single longest hypothesis.
const RUSSELL_REAL = (
  "okayokayokayokay" +
  "okay I'mokay I'mokay I'm" +
  "okay I'm testing" +
  "okay I'm testing theokay I'm testing theokay I'm testing the" +
  "okay I'm testing the microphoneokay I'm testing the microphoneokay I'm testing the microphone" +
  "okay I'm testing the microphone and" +
  "okay I'm testing the microphone and at" +
  "okay I'm testing the microphone and at the sameokay I'm testing the microphone and at the same" +
  "okay I'm testing the microphone and at the same timeokay I'm testing the microphone and at the same time" +
  "okay I'm testing the microphone and at the same time telling" +
  "okay I'm testing the microphone and at the same time telling youokay I'm testing the microphone and at the same time telling youokay I'm testing the microphone and at the same time telling you" +
  "okay I'm testing the microphone and at the same time telling you I" +
  "okay I'm testing the microphone and at the same time telling you I knewokay I'm testing the microphone and at the same time telling you I knewokay I'm testing the microphone and at the same time telling you I knewokay I'm testing the microphone and at the same time telling you I knewokay I'm testing the microphone and at the same time telling you I knew" +
  "okay I'm testing the microphone and at the same time telling you I knew meta rule"
)
const REAL_ENTRIES = RUSSELL_REAL.split(/(?=okay)/).filter(Boolean)
const REAL_FINAL = "okay I'm testing the microphone and at the same time telling you I knew meta rule"

describe('REAL device pattern from ticket 3499 (partials repeat up to 5x)', () => {
  it('sanity: 31 entries, concatenation reproduces the stored garbled message', () => {
    expect(REAL_ENTRIES.length).toBe(31)
    expect(REAL_ENTRIES.join('')).toBe(RUSSELL_REAL)
  })
  it('fixed hook: one event with all 31 interim entries -> one clean sentence', () => {
    const ev = REAL_ENTRIES.map(t => ({ t, f: false }))
    expect(run(useSpeechRecognition, [ev]).final).toBe(REAL_FINAL)
  })
  it('fixed hook: first 4 bare "okay" marked final, rest interim -> clean', () => {
    const ev = REAL_ENTRIES.map((t, i) => ({ t, f: i < 4 }))
    expect(run(useSpeechRecognition, [ev]).final).toBe(REAL_FINAL)
  })
  it('fixed hook: growing event stream (each event adds one entry) -> clean at every step', () => {
    const evs = REAL_ENTRIES.map((_, i) => REAL_ENTRIES.slice(0, i + 1).map(t => ({ t, f: false })))
    const { final, seen } = run(useSpeechRecognition, evs)
    expect(final).toBe(REAL_FINAL)
    seen.forEach(s => expect(REAL_FINAL.startsWith(s)).toBe(true))
  })
  it('NEGATIVE CONTROL: old hook reproduces Russell\'s exact garbled message byte-for-byte', () => {
    const ev = REAL_ENTRIES.map(t => ({ t, f: false }))
    expect(run(legacyUseSpeechRecognition as any, [ev]).final).toBe(RUSSELL_REAL)
  })
})
