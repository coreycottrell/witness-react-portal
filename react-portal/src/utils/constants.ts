export const AUTH_TOKEN_KEY = 'aiciv-portal-token'
export const THEME_KEY = 'aiciv-theme'
export const SETTINGS_KEY = 'aiciv-settings'

// P20 operator identity — WHO is at the portal right now. Tagged onto every
// message so Primary always knows who is speaking, and so every operator sees
// correct attribution. Quick-pick names + free-text "Other".
export const OPERATOR_KEY = 'aiciv-portal-operator'
export const OPERATOR_OPTIONS = ['Corey', 'Russell', 'Jared'] as const

// Quick-action "quickfire pills" removed 2026-09-07 (Russell/Corey): the pills
// above the chat input were too easy to hit by accident (one fired a Status
// Report by mistake). The chat render block was removed from ChatInput.tsx and
// this default emptied so no default pills exist anywhere. Users can still add
// their own in Settings -> Quick Fire Messages. Reversible: restore the four
// strings ('Status report','Check schedule','Read inbox','What are you working on?').
export const DEFAULT_QUICKFIRE_PILLS: string[] = []

export const DAYS_OF_WEEK = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'] as const
export const RECUR_DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'] as const
