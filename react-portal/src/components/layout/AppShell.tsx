import { Outlet } from 'react-router-dom'
import { Header } from './Header'
import { Sidebar } from './Sidebar'
import { MobileNav } from './MobileNav'
import { OperatorGate } from '../operator/OperatorGate'
import './AppShell.css'

export function AppShell() {
  return (
    <div className="app-shell">
      {/* P20: prompt WHO is at the portal, tag every message with their name */}
      <OperatorGate />
      <Header />
      <div className="app-body">
        <Sidebar />
        <main className="app-main">
          <Outlet />
        </main>
      </div>
      <MobileNav />
    </div>
  )
}
