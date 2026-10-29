// Apply the stored theme before first paint to avoid a light/dark flash. A file rather
// than an inline script so the Content-Security-Policy can forbid inline scripts.
// Mirrors ThemeProvider (src/app/contexts/theme.tsx).
try {
  var t = localStorage.getItem('inquest.theme')
  var dark = t === 'dark' || (t !== 'light' && matchMedia('(prefers-color-scheme: dark)').matches)
  if (dark) document.documentElement.classList.add('dark')
} catch (e) {}
