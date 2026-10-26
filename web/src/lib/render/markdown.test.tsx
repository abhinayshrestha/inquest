import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { Markdown } from './markdown'

describe('Markdown', () => {
  it('renders GFM tables and highlights fenced code with Shiki tokens, never as markup', async () => {
    const { container } = render(
      <Markdown>
        {
          '| a | b |\n| --- | --- |\n| 1 | 2 |\n\n```json\n{"p95_ms": 1514, "x": "<img src=x onerror=alert(1)>"}\n```'
        }
      </Markdown>,
    )
    expect(screen.getByRole('table')).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: '2' })).toBeInTheDocument()
    const code = container.querySelector('pre code')!
    // Plain text first, then tokens with theme variables once Shiki has loaded.
    await waitFor(() => expect(code).toHaveAttribute('data-highlighted', 'true'), { timeout: 5000 })
    const token = [...code.querySelectorAll('span')].find((s) => s.textContent === '"p95_ms"')
    expect(token?.getAttribute('style')).toMatch(/--shiki-light/)
    expect(code.textContent).toBe('{"p95_ms": 1514, "x": "<img src=x onerror=alert(1)>"}')
    expect(container.querySelector('img')).toBeNull()
  })

  it('never renders raw HTML, scripts or event handlers', () => {
    const { container } = render(
      <Markdown>
        {
          'Hello <script>window.pwned=1</script><img src=x onerror="alert(1)"> <b>bold</b>\n\n<iframe src="https://evil.example"></iframe>'
        }
      </Markdown>,
    )
    expect(container.querySelector('script, iframe, b, img')).toBeNull()
    expect(container.innerHTML).not.toMatch(/onerror|<script|<iframe/i)
    expect(container).toHaveTextContent('Hello')
  })

  it('drops unsafe link protocols and opens safe links in a new tab', () => {
    render(
      <Markdown>
        {
          '[x](javascript:alert(1)) [d](data:text/html,<b>) [rel](/settings) [ok](https://signoz.io)'
        }
      </Markdown>,
    )
    const links = screen.getAllByRole('link')
    expect(links).toHaveLength(1)
    expect(links[0]).toHaveAttribute('href', 'https://signoz.io')
    expect(links[0]).toHaveAttribute('rel', 'noopener noreferrer nofollow')
    expect(links[0]).toHaveAttribute('target', '_blank')
    expect(screen.getByText('x')).toBeInTheDocument()
  })

  it('does not load remote images', () => {
    const { container } = render(
      <Markdown>{'![tracking pixel](https://evil.example/p.gif)'}</Markdown>,
    )
    expect(container.querySelector('img')).toBeNull()
    expect(container).toHaveTextContent('[image: tracking pixel]')
  })

  it('turns refs into citation buttons, but not inside code or links', async () => {
    const onCite = vi.fn()
    render(
      <Markdown onCite={onCite}>
        {
          'p95 ≈ 1.5 s [ev_1f5967bf, fd_46e29040] and hy_0a1b2c3d.\n\n`ev_ffffffff` [ev_eeeeeeee](https://x.example) ev_12345 xev_1f5967bfx'
        }
      </Markdown>,
    )
    const chips = screen.getAllByRole('button')
    expect(chips.map((c) => c.textContent)).toEqual(['ev_1f5967bf', 'fd_46e29040', 'hy_0a1b2c3d'])
    expect(screen.getByRole('button', { name: 'Finding fd_46e29040' })).toBeInTheDocument()
    // Brackets around a group of refs are dropped.
    expect(screen.queryByText(/\[/)).not.toBeInTheDocument()

    await userEvent.click(chips[0]!)
    expect(onCite).toHaveBeenCalledWith('ev_1f5967bf')
  })

  it('renders answers whose line breaks arrived as spaces as headings and lists', () => {
    render(
      <Markdown>
        {
          '## Investigation Summary  ### Slow endpoints  Three groups are slow:  - **GET /slow** – 28 spans  - **Handler** – 56 spans  All else is fast.'
        }
      </Markdown>,
    )
    expect(
      screen.getByRole('heading', { level: 2, name: 'Investigation Summary' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3, name: 'Slow endpoints' })).toBeInTheDocument()
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
    expect(screen.getByText('All else is fast.').tagName).toBe('P')
  })

  it('collapses long citation runs and expands them on request', async () => {
    const onCite = vi.fn()
    render(
      <Markdown onCite={onCite}>
        {'Summary [ev_00000001, ev_00000002, ev_00000003, ev_00000004, fd_00000005, fd_00000006].'}
      </Markdown>,
    )
    expect(screen.getAllByRole('button', { name: /^(Evidence|Finding) / })).toHaveLength(2)
    await userEvent.click(screen.getByRole('button', { name: 'Show 4 more citations' }))
    expect(screen.getAllByRole('button', { name: /^(Evidence|Finding) / })).toHaveLength(6)
    await userEvent.click(screen.getByRole('button', { name: 'Finding fd_00000006' }))
    expect(onCite).toHaveBeenCalledWith('fd_00000006')
  })
})
