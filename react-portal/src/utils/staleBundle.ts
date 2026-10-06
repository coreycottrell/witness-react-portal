// Stale-bundle self-heal (ticket 3499, 2026-10-06).
//
// WHY: the mobile-mic fix shipped as a new content-hashed bundle (index-DGqcZShr.js), but a
// phone tab that was already open (or whose index.html was served from the browser's heuristic
// cache — the server sent NO Cache-Control) kept running the OLD bundle, and the old dictation
// bug reappeared even though the fix was live. A fix a user's open tab never picks up is not
// shipped. This guard makes every NEW bundle detect that the server now serves a newer one and
// reload itself — but only when that cannot destroy work (nothing typed, mic idle).

const BUNDLE_RE = /\/assets\/(index-[A-Za-z0-9_-]+\.js)/

/** Pull the entry bundle filename out of an index.html (or out of a module URL). */
export function extractBundleName(text: string): string | null {
  const m = BUNDLE_RE.exec(text)
  return m ? m[1] : null
}

/** True only when both names are known and differ — unknown never triggers a reload. */
export function isStaleBundle(running: string | null, served: string | null): boolean {
  return !!running && !!served && running !== served
}

/** True when a reload would not lose the operator's work. */
export function safeToReload(doc: Document = document): boolean {
  const fields = doc.querySelectorAll<HTMLTextAreaElement | HTMLInputElement>('textarea, input[type="text"], input:not([type])')
  for (const f of Array.from(fields)) if ((f.value || '').trim()) return false
  if (doc.querySelector('.chat-mic-active')) return false   // dictation in progress
  return true
}

const RELOAD_KEY = 'portal-stale-reload-target'

export async function checkAndReload(runningName: string | null): Promise<boolean> {
  try {
    const res = await fetch(`/react?_=${Date.now()}`, { cache: 'no-store' })
    if (!res.ok) return false
    const served = extractBundleName(await res.text())
    if (!isStaleBundle(runningName, served)) return false
    if (!safeToReload()) return false
    // Loop guard: reload at most once per target bundle (a proxy serving old HTML must not loop us).
    try {
      if (sessionStorage.getItem(RELOAD_KEY) === served) return false
      sessionStorage.setItem(RELOAD_KEY, served as string)
    } catch { /* storage blocked: fall through, a single reload is still fine */ }
    window.location.reload()
    return true
  } catch {
    return false
  }
}

export function installStaleBundleGuard(): void {
  if (typeof window === 'undefined') return
  const running = extractBundleName(import.meta.url)
  if (!running) return                       // dev server / unhashed: nothing to compare
  const run = () => { void checkAndReload(running) }
  setTimeout(run, 3000)
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') run() })
  window.addEventListener('pageshow', e => { if ((e as PageTransitionEvent).persisted) run() })
  window.addEventListener('online', run)
  setInterval(run, 5 * 60 * 1000)
}
