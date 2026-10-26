import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { ErrorBoundary } from './error-boundary'

let shouldThrow = true

function Flaky() {
  if (shouldThrow) throw new Error('boom')
  return <p>recovered</p>
}

describe('ErrorBoundary', () => {
  it('renders a fallback and recovers on retry', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    shouldThrow = true
    render(
      <ErrorBoundary>
        <Flaky />
      </ErrorBoundary>,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('Something went wrong')
    // The raw error message is not shown to users.
    expect(screen.queryByText('boom')).not.toBeInTheDocument()

    shouldThrow = false
    await userEvent.click(screen.getByRole('button', { name: /try again/i }))
    expect(screen.getByText('recovered')).toBeInTheDocument()
  })

  it('resets when a reset key changes', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    shouldThrow = true
    function Harness() {
      const [key, setKey] = useState(0)
      return (
        <>
          <button onClick={() => setKey((k) => k + 1)}>navigate</button>
          <ErrorBoundary resetKeys={[key]}>
            <Flaky />
          </ErrorBoundary>
        </>
      )
    }
    render(<Harness />)
    expect(screen.getByRole('alert')).toBeInTheDocument()
    shouldThrow = false
    await userEvent.click(screen.getByRole('button', { name: 'navigate' }))
    expect(screen.getByText('recovered')).toBeInTheDocument()
  })
})
