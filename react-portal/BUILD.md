# react-portal build steps

1. `npm ci` (or `npm install`)
2. `npm run build`
   - `prebuild` runs automatically first: `npm run test:mic` (vitest, `src/test/useSpeechRecognition.mobile.test.ts`).
     This is the mobile-microphone regression guard. It replays Android/iOS cumulative
     `SpeechRecognition` result sequences and fails the build if dictation text is duplicated
     ("clearlyclearly theclearly the ..."). A failing guard blocks `tsc -b && vite build`.
   - then `tsc -b && vite build` -> `dist/`
3. Swap `dist/` into place (portal_server.py serves `react-portal/dist` from disk per request; no restart).
4. Before deploying a build, you may run the full suite: `npm test`.

Do NOT build from a working tree that is on a branch lacking the voice fix (see git log for
`fix(portal): mobile mic`). The fix was lost twice (2026-09-23 checkout, 2026-10-05 rebuild).
Quickfire pills above the chat input are intentionally removed (Russell/Corey 2026-09-07/08).
