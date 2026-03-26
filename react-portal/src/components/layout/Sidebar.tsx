import { NavLink } from 'react-router-dom'
import { useMailStore } from '../../stores/mailStore'
import { useBookmarkStore } from '../../stores/bookmarkStore'
import { cn } from '../../utils/cn'
import { WITNESS_NAV_ITEMS } from '../../extensions'
import './Sidebar.css'

const NAV_ITEMS = [
  { to: '/', icon: '\u{1F4AC}', label: 'Chat' },
  { to: '/terminal', icon: '\u{2328}\u{FE0F}', label: 'Terminal' },
  { to: '/teams', icon: '\u{1F465}', label: 'Teams' },
  { to: '/orgchart', icon: '\u{1F3E2}', label: 'Org Chart' },
  { to: '/calendar', icon: '\u{1F4C5}', label: 'AgentCal' },
  { to: '/mail', icon: '\u{1F4E8}', label: 'AgentMail' },
  { to: '/bookmarks', icon: '\u{1F4CC}', label: 'Bookmarks' },
  { to: '/context', icon: '\u{1F9E0}', label: 'Context' },
  { to: '/points', icon: '\u{2B50}', label: 'Points' },
  { to: '/docs', icon: '\u{1F4D6}', label: 'Docs' },
  { to: '/sheets', icon: '\u{1F4CA}', label: 'Sheets' },
  { to: '/status', icon: '\u{1F4CA}', label: 'Status' },
  { to: '/settings', icon: '\u{2699}\u{FE0F}', label: 'Settings' },
] as const

export function Sidebar() {
  const unreadCount = useMailStore(s => s.unreadCount)
  const bookmarkCount = useBookmarkStore(s => s.bookmarks.length)

  return (
    <aside className="sidebar">
      <nav className="sidebar-nav">
        {NAV_ITEMS.map(item => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) => cn('sidebar-link', isActive && 'sidebar-link-active')}
          >
            <span className="sidebar-icon">{item.icon}</span>
            <span className="sidebar-label">{item.label}</span>
            {item.to === '/mail' && unreadCount > 0 && (
              <span className="sidebar-badge">{unreadCount}</span>
            )}
            {item.to === '/bookmarks' && bookmarkCount > 0 && (
              <span className="sidebar-badge">{bookmarkCount}</span>
            )}
          </NavLink>
        ))}
        {/* Witness extensions — injected from extensions.ts, only present in Witness's local build */}
        {WITNESS_NAV_ITEMS.map(item => (
          <NavLink key={item.to} to={item.to} className={({ isActive }) => cn('sidebar-link', isActive && 'sidebar-link-active')}>
            <span className="sidebar-icon">{item.icon}</span>
            <span className="sidebar-label">{item.label}</span>
          </NavLink>
        ))}
      </nav>
      <div className="sidebar-footer">
        <a href="https://ai-civ.com" target="_blank" rel="noopener noreferrer" className="sidebar-powered">
          Powered by <strong>AiCIV</strong>
        </a>
        <a href="https://ai-civ.com/blog" target="_blank" rel="noopener noreferrer" className="sidebar-blog-link">
          Chronicles
        </a>
      </div>
    </aside>
  )
}
