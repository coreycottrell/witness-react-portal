import { useState, useCallback } from 'react'
import { NavLink, useNavigate } from 'react-router-dom'
import { useMailStore } from '../../stores/mailStore'
import { cn } from '../../utils/cn'
import './MobileNav.css'

const PRIMARY_ITEMS = [
  { to: '/', icon: '\u{1F4AC}', label: 'Chat' },
  { to: '/calendar', icon: '\u{1F4C5}', label: 'Cal' },
  { to: '/mail', icon: '\u{1F4E8}', label: 'Mail' },
  { to: '/terminal', icon: '\u{2328}\u{FE0F}', label: 'Term' },
] as const

const MORE_ITEMS = [
  { to: '/orgchart', icon: '\u{1F3E2}', label: 'Org Chart' },
  { to: '/teams', icon: '\u{1F465}', label: 'Teams' },
  { to: '/context', icon: '\u{1F9E0}', label: 'Context' },
  { to: '/points', icon: '\u{2B50}', label: 'Points' },
  { to: '/docs', icon: '\u{1F4D6}', label: 'Docs' },
  { to: '/sheets', icon: '\u{1F4CA}', label: 'Sheets' },
  { to: '/bookmarks', icon: '\u{1F4CC}', label: 'Bookmarks' },
  { to: '/status', icon: '\u{1F4CA}', label: 'Status' },
  { to: '/settings', icon: '\u{2699}\u{FE0F}', label: 'Settings' },
] as const

export function MobileNav() {
  const unreadCount = useMailStore(s => s.unreadCount)
  const [moreOpen, setMoreOpen] = useState(false)
  const navigate = useNavigate()

  const handleMoreItem = useCallback((to: string) => {
    navigate(to)
    setMoreOpen(false)
  }, [navigate])

  return (
    <>
      {/* Overlay */}
      {moreOpen && (
        <div className="mobile-more-overlay" onClick={() => setMoreOpen(false)} />
      )}

      {/* Slide-up sheet */}
      {moreOpen && (
        <div className="mobile-more-sheet">
          <div className="mobile-more-handle" />
          <div className="mobile-more-grid">
            {MORE_ITEMS.map(item => (
              <button
                key={item.to}
                className="mobile-more-item"
                onClick={() => handleMoreItem(item.to)}
                type="button"
              >
                <span className="mobile-more-icon">{item.icon}</span>
                <span className="mobile-more-label">{item.label}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Bottom nav bar */}
      <nav className="mobile-nav">
        {PRIMARY_ITEMS.map(item => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) => cn('mobile-nav-item', isActive && 'mobile-nav-active')}
            onClick={() => setMoreOpen(false)}
          >
            <span className="mobile-nav-icon">
              {item.icon}
              {item.to === '/mail' && unreadCount > 0 && (
                <span className="mobile-nav-badge">{unreadCount}</span>
              )}
            </span>
            <span className="mobile-nav-label">{item.label}</span>
          </NavLink>
        ))}
        <button
          className={cn('mobile-nav-item', 'mobile-nav-more-btn', moreOpen && 'mobile-nav-active')}
          onClick={() => setMoreOpen(o => !o)}
          type="button"
        >
          <span className="mobile-nav-icon">{moreOpen ? '\u{2716}' : '\u{2630}'}</span>
          <span className="mobile-nav-label">More</span>
        </button>
      </nav>
    </>
  )
}
