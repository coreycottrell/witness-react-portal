import { useSettingsStore } from '../../stores/settingsStore'
import { useAuthStore } from '../../stores/authStore'
import { useIdentityStore } from '../../stores/identityStore'
import { toggleBoop } from '../../api/settings'
import { cn } from '../../utils/cn'
import type { Theme } from '../../types/settings'
import './SettingsView.css'

export function SettingsView() {
  const { theme, setTheme, quickfirePills, setQuickfirePills, boopEnabled, setBoopEnabled } = useSettingsStore()
  const { logout } = useAuthStore()
  const { civName, humanName, status } = useIdentityStore()

  const handleThemeToggle = (t: Theme) => {
    setTheme(t)
  }

  const handleBoopToggle = async () => {
    const next = !boopEnabled
    try {
      await toggleBoop(next)
      setBoopEnabled(next)
    } catch {
      // silently fail
    }
  }

  const handleRemovePill = (pill: string) => {
    setQuickfirePills(quickfirePills.filter(p => p !== pill))
  }

  const handleAddPill = () => {
    const val = prompt('Enter quickfire message:')
    if (val?.trim() && !quickfirePills.includes(val.trim())) {
      setQuickfirePills([...quickfirePills, val.trim()])
    }
  }

  return (
    <div className="settings-view">
      <h2 className="settings-title">Settings</h2>

      <section className="settings-section">
        <h3>Identity</h3>
        <div className="settings-info">
          <div className="settings-row">
            <span className="settings-label">CIV Name</span>
            <span className="settings-value">{civName || '—'}</span>
          </div>
          <div className="settings-row">
            <span className="settings-label">Human Name</span>
            <span className="settings-value">{humanName || '—'}</span>
          </div>
          <div className="settings-row">
            <span className="settings-label">Version</span>
            <span className="settings-value">{status?.version || '—'}</span>
          </div>
        </div>
      </section>

      <section className="settings-section">
        <h3>Appearance</h3>
        <div className="theme-toggle">
          {(['dark', 'light'] as Theme[]).map(t => (
            <button
              key={t}
              className={cn('theme-btn', theme === t && 'theme-btn-active')}
              onClick={() => handleThemeToggle(t)}
            >
              {t === 'dark' ? 'Dark' : 'Light'}
            </button>
          ))}
        </div>
      </section>

      <section className="settings-section">
        <h3>BOOP</h3>
        <div className="settings-row">
          <span className="settings-label">Background tasks</span>
          <button
            className={cn('boop-toggle', boopEnabled && 'boop-toggle-on')}
            onClick={handleBoopToggle}
          >
            <span className="boop-toggle-thumb" />
          </button>
        </div>
      </section>

      <section className="settings-section">
        <h3>Quick Fire Messages</h3>
        <div className="pill-list">
          {quickfirePills.map((pill, i) => (
            <span key={`${i}-${pill}`} className="pill-item">
              {pill}
              <button className="pill-remove" onClick={() => handleRemovePill(pill)}>&times;</button>
            </span>
          ))}
          <button className="pill-add" onClick={handleAddPill}>+ Add</button>
        </div>
      </section>

      <section className="settings-section">
        <h3>Resources</h3>
        <div className="settings-links">
          <a href="https://ai-civ.com" target="_blank" rel="noopener noreferrer" className="settings-link">
            AiCIV Platform
          </a>
          <a href="https://ai-civ.com/blog" target="_blank" rel="noopener noreferrer" className="settings-link">
            AiCIV Chronicles (Blog)
          </a>
        </div>
      </section>

      <section className="settings-section">
        <button className="settings-logout" onClick={logout}>
          Logout
        </button>
      </section>
    </div>
  )
}
