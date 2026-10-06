import { describe, it, expect, beforeEach } from 'vitest'
import { extractBundleName, isStaleBundle, safeToReload } from '../utils/staleBundle'

const NEW_HTML = '<script type="module" crossorigin src="/react/assets/index-DGqcZShr.js"></script>'

describe('stale bundle detection', () => {
  it('extracts the entry bundle from index.html and from a module URL', () => {
    expect(extractBundleName(NEW_HTML)).toBe('index-DGqcZShr.js')
    expect(extractBundleName('https://x.test/react/assets/index-DyaS-QFC.js')).toBe('index-DyaS-QFC.js')
    expect(extractBundleName('/src/main.tsx')).toBeNull()
  })
  it('old tab (DyaS-QFC) vs served new bundle -> stale', () => {
    expect(isStaleBundle('index-DyaS-QFC.js', extractBundleName(NEW_HTML))).toBe(true)
  })
  it('same bundle, or either side unknown -> not stale (never reload on doubt)', () => {
    expect(isStaleBundle('index-DGqcZShr.js', 'index-DGqcZShr.js')).toBe(false)
    expect(isStaleBundle(null, 'index-DGqcZShr.js')).toBe(false)
    expect(isStaleBundle('index-DGqcZShr.js', null)).toBe(false)
  })
})

describe('safeToReload never destroys work', () => {
  beforeEach(() => { document.body.innerHTML = '' })
  it('true on an idle empty page', () => {
    document.body.innerHTML = '<textarea></textarea>'
    expect(safeToReload()).toBe(true)
  })
  it('false when a draft is typed', () => {
    document.body.innerHTML = '<textarea>half a message</textarea>'
    expect(safeToReload()).toBe(false)
  })
  it('false while dictation is active', () => {
    document.body.innerHTML = '<textarea></textarea><button class="chat-mic-btn chat-mic-active"></button>'
    expect(safeToReload()).toBe(false)
  })
})
