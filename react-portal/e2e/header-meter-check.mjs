#!/usr/bin/env node
// Phone-visibility guard for the header usage meter (Russell 2026-10-28: "the meter is not visible on mobile").
//
// Loads the portal in WebKit and Chromium at real phone widths (default 360,375,390,414,430), portrait and
// landscape, dark and light theme, and FAILS (exit 1) if, at any of them, the meter is
//   - hidden, transparent, zero-size, or covered by another element;
//   - not entirely inside the viewport, or the page / header content overflows sideways (a clipped badge counts);
//   - too small or too faint to read (font < 11px, or text contrast < 4.5:1 against the header);
//   - missing the account line, or its tap target (>= 24px tall) does not open the details inside the viewport.
// It only loads pages and taps the meter: it never calls the auth endpoints (they are aborted if requested).
//
// Run:  PLAYWRIGHT_CORE=/path/to/node_modules BASE=http://127.0.0.1:8097 TOKEN_FILE=/path/.portal-token \
//         OUT=/tmp/shots node e2e/header-meter-check.mjs
//   BASE        portal origin (default http://127.0.0.1:8097)
//   TOKEN / TOKEN_FILE  portal bearer token (read in-process, never printed); any value works against a mock
//   ENGINES     chromium,webkit   WIDTHS 360,375,390,414,430   THEMES dark,light   OUT screenshot dir
//   CHROME_PATH use a system Chrome instead of Playwright's Chromium;  PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1 to skip host checks
import { createRequire } from 'module'
import fs from 'fs'

const require = createRequire((process.env.PLAYWRIGHT_CORE || process.cwd() + '/node_modules') + '/')
const pw = require('playwright-core')
const BASE = process.env.BASE || 'http://127.0.0.1:8097'
const OUT = process.env.OUT || './header-meter-shots'
const TOKEN = process.env.TOKEN || (process.env.TOKEN_FILE ? fs.readFileSync(process.env.TOKEN_FILE, 'utf8').trim() : 'mock')
const widths = (process.env.WIDTHS || '360,375,390,414,430').split(',').map(Number)
const engines = (process.env.ENGINES || 'chromium,webkit').split(',')
const themes = (process.env.THEMES || 'dark,light').split(',')
const tag = process.env.TAG || 'run'
fs.mkdirSync(OUT, { recursive: true })

const UA = {
  webkit: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1',
  chromium: 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36',
}

// in-page measurement (runs in the browser)
function measure() {
  const vw = window.innerWidth
  const q = s => document.querySelector(s)
  const meter = q('[data-testid=claude-usage]')
  const header = q('header.header')
  const rect = el => { if (!el) return null; const b = el.getBoundingClientRect(); return { x: b.left, r: b.right, y: b.top, b: b.bottom, w: b.width, h: b.height } }
  const parse = c => { const m = /rgba?\(([^)]+)\)/.exec(c || ''); if (!m) return null; const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number); return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 } }
  const lum = ({ r, g, b }) => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4) }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b) }
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05) }
  const bgOf = el => { for (let e = el; e; e = e.parentElement) { const c = parse(getComputedStyle(e).backgroundColor); if (c && c.a > 0.5) return c } return { r: 255, g: 255, b: 255, a: 1 } }
  const out = { vw, docOverflow: document.documentElement.scrollWidth - vw }
  out.meter = !!meter
  if (!meter) return out
  const cs = getComputedStyle(meter)
  out.hidden = cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity) === 0
  out.rect = rect(meter)
  out.text = meter.innerText.replace(/\s+/g, ' ').trim()
  const b = meter.getBoundingClientRect()
  const top = document.elementFromPoint(Math.min(Math.max(b.left + b.width / 2, 0), vw - 1), b.top + b.height / 2)
  out.covered = top && !(meter.contains(top) || top.contains(meter)) ? String(top.className || top.tagName) : null
  const email = q('[data-testid=claude-usage-email]'), bar = q('[data-testid=claude-usage-5h] .claude-usage-text') || q('[data-testid=claude-usage-na]')
  const dim = el => { if (!el) return null; const s = getComputedStyle(el), bg = bgOf(el); return { font: parseFloat(s.fontSize), contrast: Number(ratio(parse(s.color), bg).toFixed(2)), color: s.color } }
  out.email = dim(email); out.bar = dim(bar)
  out.emailText = email ? email.innerText : null
  const kids = header ? [...header.querySelectorAll('.header-left, .header-right > *, .header-right > * *')].filter(e => e.getBoundingClientRect().width > 0) : []
  out.headerOverflow = header ? Math.max(0, header.scrollWidth - header.clientWidth) : null
  out.kidsOutside = kids.filter(e => { const r = e.getBoundingClientRect(); return r.right > vw + 0.5 || r.left < -0.5 }).map(e => String(e.className || e.tagName).slice(0, 40))
  out.headerBottom = header ? header.getBoundingClientRect().bottom : null
  return out
}

const results = []
let failures = 0
for (const eng of engines) {
  let browser
  try {
    browser = await pw[eng].launch(eng === 'chromium' && process.env.CHROME_PATH ? { headless: true, executablePath: process.env.CHROME_PATH } : { headless: true })
  } catch (e) { console.log(eng, 'LAUNCH FAILED', String(e).split('\n')[0]); failures++; continue }
  for (const theme of themes) for (const w of widths) for (const orient of ['portrait', 'landscape']) {
    const h = Math.round(w * 2.16)
    const vp = orient === 'portrait' ? { width: w, height: h } : { width: h, height: w }
    const ctx = await browser.newContext({ viewport: vp, deviceScaleFactor: 3, isMobile: true, hasTouch: true, userAgent: UA[eng] })
    await ctx.addInitScript(([t, th]) => {
      try { localStorage.setItem('aiciv-portal-token', t); localStorage.setItem('aiciv-portal-operator', 'layout-check'); localStorage.setItem('aiciv-theme', th) } catch (e) {}
    }, [TOKEN, theme])
    const page = await ctx.newPage()
    await page.route(/\/api\/auth\/(start|code|url)/, r => r.abort())
    const row = { eng, theme, w, orient, vp: `${vp.width}x${vp.height}`, problems: [] }
    try {
      await page.goto(BASE + '/', { waitUntil: 'load', timeout: 30000 })
      await page.waitForSelector('header.header', { timeout: 15000 })
      await page.waitForSelector('[data-testid=claude-usage-email], [data-testid=claude-usage]', { timeout: 15000 }).catch(() => {})
      await page.waitForTimeout(2500)
      const m = await page.evaluate(measure)
      Object.assign(row, m)
      const P = row.problems
      if (!m.meter) P.push('meter not rendered')
      else {
        if (m.hidden) P.push('meter hidden (display/visibility/opacity)')
        if (m.rect.w < 40 || m.rect.h < 8) P.push(`meter has no size ${Math.round(m.rect.w)}x${Math.round(m.rect.h)}`)
        if (m.rect.x < -0.5 || m.rect.r > m.vw + 0.5) P.push(`meter outside viewport x=${Math.round(m.rect.x)}..${Math.round(m.rect.r)} of ${m.vw}`)
        if (m.rect.h < 24) P.push(`tap target ${Math.round(m.rect.h)}px tall (<24)`)
        if (m.covered) P.push('meter covered by ' + m.covered)
        if (m.docOverflow > 0) P.push(`page scrolls sideways by ${m.docOverflow}px`)
        if (m.headerOverflow > 0) P.push(`header content overflows by ${m.headerOverflow}px (something is clipped)`)
        if (m.kidsOutside.length) P.push('header items outside viewport: ' + m.kidsOutside.join(','))
        for (const [name, d] of [['account', m.email], ['usage text', m.bar]]) {
          if (!d) { P.push(`${name} line missing`); continue }
          if (d.font < 11) P.push(`${name} font ${d.font}px (<11)`)
          if (d.contrast < 4.5) P.push(`${name} contrast ${d.contrast}:1 (<4.5)`)
        }
        // the shot is taken BEFORE tapping, so it shows the resting header
        const f = `${OUT}/${tag}-${eng}-${theme}-${w}-${orient}.png`
        await page.screenshot({ path: f, clip: { x: 0, y: 0, width: vp.width, height: Math.min(vp.height, 170) } })
        row.shot = f
        await page.tap('[data-testid=claude-usage]').catch(() => {})
        await page.waitForTimeout(300)
        const pop = await page.evaluate(() => { const p = document.querySelector('[data-testid=claude-usage-popover]'); if (!p) return null; const b = p.getBoundingClientRect(); return { x: b.left, r: b.right, b: b.bottom, vw: window.innerWidth, vh: window.innerHeight } })
        if (!pop) P.push('tap did not open the details')
        else if (pop.x < -0.5 || pop.r > pop.vw + 0.5 || pop.b > pop.vh + 0.5) P.push(`details outside viewport (${Math.round(pop.x)}..${Math.round(pop.r)} / bottom ${Math.round(pop.b)} of ${pop.vw}x${pop.vh})`)
        else await page.screenshot({ path: `${OUT}/${tag}-${eng}-${theme}-${w}-${orient}-open.png`, clip: { x: 0, y: 0, width: vp.width, height: Math.min(vp.height, 330) } })
      }
    } catch (e) { row.problems.push('error: ' + String(e).split('\n')[0].slice(0, 200)) }
    if (row.problems.length) failures++
    results.push(row)
    console.log(`${row.problems.length ? 'FAIL' : 'ok  '} ${eng.padEnd(8)} ${theme.padEnd(5)} ${row.vp.padEnd(8)} ${orient.padEnd(9)} ${row.text ? JSON.stringify(row.text) : ''}${row.problems.length ? '\n       - ' + row.problems.join('\n       - ') : ''}`)
    await ctx.close()
  }
  await browser.close()
}
fs.writeFileSync(`${OUT}/${tag}-results.json`, JSON.stringify(results, null, 1))
console.log(`\n${results.length - results.filter(r => r.problems.length).length}/${results.length} viewport combinations pass`)
process.exit(failures ? 1 : 0)
