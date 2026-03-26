import { lazy, Suspense, useEffect } from 'react'
import { HashRouter, Routes, Route } from 'react-router-dom'
// Witness extension registry — Witness-only routes layered on top of base portal
import { WITNESS_ROUTES } from './extensions'
// Pre-create lazy components at module level (NOT inside render) to avoid
// remount loops — React.lazy must return a stable reference across renders.
const WITNESS_PANELS = WITNESS_ROUTES.map(r => ({
  path: r.path,
  Component: lazy(r.component),
}))
import { AuthGuard } from './components/auth/AuthGuard'
import { ClaudeAuthFlow } from './components/auth/ClaudeAuthFlow'
import { AppShell } from './components/layout/AppShell'
import { ChatView } from './components/chat/ChatView'
import { CalendarView } from './components/calendar/CalendarView'
import { MailView } from './components/agentmail/MailView'
import { SettingsView } from './components/settings/SettingsView'
import { TerminalView } from './components/terminal/TerminalView'
import { TeamsView } from './components/teams/TeamsView'
import { BookmarksView } from './components/bookmarks/BookmarksView'
import { StatusView } from './components/status/StatusView'
import { ContextView } from './components/context/ContextView'
import OrgChartView from './components/agents/OrgChartView'
import { DocsView } from './components/docs/DocsView'
import { SheetsView } from './components/sheets/SheetsView'
import { PointsView } from './components/points/PointsView'
import { useIdentityStore } from './stores/identityStore'
import { useSettingsStore } from './stores/settingsStore'

/** Runs identity + status fetches only after auth succeeds */
function AuthenticatedApp() {
  const fetchIdentity = useIdentityStore(s => s.fetchIdentity)
  const fetchStatusInfo = useIdentityStore(s => s.fetchStatusInfo)

  useEffect(() => {
    fetchIdentity()
    fetchStatusInfo()
    const interval = setInterval(fetchStatusInfo, 30_000)
    return () => clearInterval(interval)
  }, [fetchIdentity, fetchStatusInfo])

  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route path="/" element={<ChatView />} />
        <Route path="/terminal" element={<TerminalView />} />
        <Route path="/teams" element={<TeamsView />} />
        <Route path="/orgchart" element={<OrgChartView />} />
        <Route path="/calendar" element={<CalendarView />} />
        <Route path="/mail" element={<MailView />} />
        <Route path="/bookmarks" element={<BookmarksView />} />
        <Route path="/context" element={<ContextView />} />
        <Route path="/points" element={<PointsView />} />
        <Route path="/docs" element={<DocsView />} />
        <Route path="/sheets" element={<SheetsView />} />
        <Route path="/status" element={<StatusView />} />
        <Route path="/settings" element={<SettingsView />} />
        {/* Witness extensions — lazy-loaded, only present in Witness's local build */}
        {WITNESS_PANELS.map(({ path, Component }) => (
          <Route key={path} path={path} element={<Suspense fallback={<div className="witness-panel">Loading...</div>}><Component /></Suspense>} />
        ))}
      </Route>
    </Routes>
  )
}

export default function App() {
  const loadFromStorage = useSettingsStore(s => s.loadFromStorage)

  useEffect(() => {
    loadFromStorage()
  }, [loadFromStorage])

  return (
    <HashRouter>
      <AuthGuard>
        <>
          <ClaudeAuthFlow />
          <AuthenticatedApp />
        </>
      </AuthGuard>
    </HashRouter>
  )
}
