import { act, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { ThemeProvider, useTheme } from './theme'

function Probe() {
  const { theme, resolvedTheme, setTheme } = useTheme()
  return (
    <>
      <span data-testid="theme">
        {theme}/{resolvedTheme}
      </span>
      <button onClick={() => setTheme('dark')}>dark</button>
    </>
  )
}

describe('ThemeProvider', () => {
  it('defaults to the system theme and persists an explicit choice', () => {
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    )
    expect(screen.getByTestId('theme')).toHaveTextContent('system/light')
    expect(document.documentElement).not.toHaveClass('dark')

    act(() => screen.getByRole('button', { name: 'dark' }).click())

    expect(screen.getByTestId('theme')).toHaveTextContent('dark/dark')
    expect(document.documentElement).toHaveClass('dark')
    expect(window.localStorage.getItem('inquest.theme')).toBe('dark')
  })

  it('restores the stored theme', () => {
    window.localStorage.setItem('inquest.theme', 'dark')
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    )
    expect(screen.getByTestId('theme')).toHaveTextContent('dark/dark')
  })
})
