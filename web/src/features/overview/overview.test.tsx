import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { ENV_ITEM, listItem, ORGANIZATION_ID, overviewCounts } from '@/test/fixtures'
import { mockFetch, renderRoute, requestsTo, type Reply } from '@/test/render'

function backend(extra: Record<string, Reply | Reply[]> = {}) {
  return mockFetch({
    'GET /health': { body: { status: 'ok', database: 'ok' } },
    'GET /overview': { body: overviewCounts },
    'GET /environments': { body: [ENV_ITEM] },
    'GET /investigations': { body: { items: [], next_offset: null } },
    'GET /investigations?limit=5&offset=0&latest_turn_status=completed': {
      body: { items: [listItem], next_offset: null },
    },
    'GET /investigations?limit=5&offset=0&latest_turn_status=failed&latest_turn_status=cancelled': {
      body: {
        items: [{ ...listItem, id: '00000000-0000-4000-8000-00000000000f', title: 'Broke', latest_turn_status: 'failed' }],
        next_offset: null,
      },
    },
    ...extra,
  }) // prettier-ignore
}

describe('overview dashboard', () => {
  it('shows stored counts grouped by investigation and latest-question status', async () => {
    backend()
    renderRoute('/')
    expect(
      await screen.findByRole('link', {
        name: /Investigations 11 1 running now · 17 questions asked/,
      }),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Environments 1 1 active/ })).toHaveAttribute(
      'href',
      '/environments',
    )
    expect(
      screen.getByRole('link', { name: /Report versions 5 across 3 investigations/ }),
    ).toBeInTheDocument()

    const byLatest = screen
      .getByText('Latest question per investigation')
      .closest('[data-slot="card"]') as HTMLElement
    const value = (label: string) =>
      within(byLatest).getByText(label).closest('div')!.querySelector('dd')!.textContent
    expect([value('Answered'), value('Failed'), value('Cancelled'), value('Running')]).toEqual([
      '3',
      '2',
      '5',
      '1',
    ])
    // No health scores, uptime or alert counts are shown.
    expect(screen.queryByText(/uptime|health score|alerts?/i)).not.toBeInTheDocument()
  })

  it('lists recent running, answered and failed investigations with the right filters', async () => {
    const fetchMock = backend()
    renderRoute('/')
    expect(await screen.findByText('Nothing is running.')).toBeInTheDocument()
    const answered = await screen.findByRole('list', { name: 'Recently answered' })
    expect(within(answered).getByText('API latency on /slow')).toBeInTheDocument()
    const failed = await screen.findByRole('list', { name: 'Last question failed or cancelled' })
    expect(within(failed).getByText('Last question failed')).toBeInTheDocument()

    const queries = requestsTo(fetchMock, 'GET', '/investigations').map(([input]) =>
      new URL(String(input), 'http://x').searchParams.toString(),
    )
    expect(queries).toContain(
      `limit=5&offset=0&organization_id=${ORGANIZATION_ID}&status=investigating&status=correlating`,
    )
  })

  it('shows an empty state with a way to start when nothing exists', async () => {
    backend({
      'GET /overview': {
        body: {
          ...overviewCounts,
          investigations_total: 0,
          investigations_by_status: Object.fromEntries(Object.keys(overviewCounts.investigations_by_status).map((k) => [k, 0])),
        },
      },
    }) // prettier-ignore
    renderRoute('/')
    expect(await screen.findByText('No investigations yet')).toBeInTheDocument()
  })

  it('retries when the counts fail to load', async () => {
    backend({
      'GET /overview': [
        { status: 503, body: { detail: 'database unavailable' } },
        { body: overviewCounts },
      ],
    })
    renderRoute('/')
    await userEvent.click((await screen.findAllByRole('button', { name: /try again/i }))[0]!)
    expect(await screen.findByRole('link', { name: /Investigations 11/ })).toBeInTheDocument()
  })
})
