/**
 * Witness Extension Registry
 *
 * This file declares all Witness-specific routes and sidebar items that are
 * layered on top of Synth's base portal. Keep this file as the SINGLE source
 * of truth for what Witness adds — makes upstream diffs clean.
 *
 * Pattern: Synth's App.tsx and Sidebar.tsx import from this file (3-5 line
 * patch each). We never modify Synth's component logic, only add to it.
 */

export interface WitnessRoute {
  path: string
  /** Dynamic import for code-splitting */
  component: () => Promise<{ default: React.ComponentType }>
}

export interface WitnessNavItem {
  to: string
  icon: string
  label: string
  /** Only show this nav item when WITNESS_MODE env var is set */
  witnessOnly: true
}

/** Routes Witness adds to App.tsx */
export const WITNESS_ROUTES: WitnessRoute[] = [
  {
    path: '/witness/fleet',
    component: () => import('./components/witness/FleetPanel').then(m => ({ default: m.FleetPanel })),
  },
  {
    path: '/witness/margins',
    component: () => import('./components/witness/MarginPanel').then(m => ({ default: m.MarginPanel })),
  },
  {
    path: '/witness/alerts',
    component: () => import('./components/witness/AlertsPanel').then(m => ({ default: m.AlertsPanel })),
  },
]

/** Sidebar nav items Witness adds to Sidebar.tsx */
export const WITNESS_NAV_ITEMS: WitnessNavItem[] = [
  { to: '/witness/fleet', icon: '🚢', label: 'Fleet', witnessOnly: true },
  { to: '/witness/margins', icon: '📈', label: 'Margins', witnessOnly: true },
  { to: '/witness/alerts', icon: '🔔', label: 'Alerts', witnessOnly: true },
]
