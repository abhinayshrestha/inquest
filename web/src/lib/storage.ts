// UI preferences only (theme, sidebar). Never credentials, tokens or server data.
// Storage can be unavailable (private mode, blocked site data); preferences then fall
// back to defaults instead of breaking the app.

export function readPreference(key: string): string | null {
  try {
    return window.localStorage.getItem(`inquest.${key}`)
  } catch {
    return null
  }
}

export function writePreference(key: string, value: string): void {
  try {
    window.localStorage.setItem(`inquest.${key}`, value)
  } catch {
    // Preference simply is not remembered.
  }
}
