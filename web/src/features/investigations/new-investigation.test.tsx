import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { ENV, ENV_ITEM, INVESTIGATION_ID, investigationPending } from '@/test/fixtures'
import { mockFetch, renderRoute, requestsTo } from '@/test/render'

const PATH = `/investigations/${INVESTIGATION_ID}`

async function fill() {
  const user = userEvent.setup()
  await user.type(await screen.findByLabelText('Title'), 'API latency')
  await user.type(screen.getByLabelText('Question'), 'Why is my API slow?')
  await user.selectOptions(screen.getByLabelText('Severity (optional)'), 'high')
  await user.click(screen.getByRole('button', { name: 'Create and start' }))
}

describe('new investigation', () => {
  it('creates and starts an investigation, then opens it', async () => {
    const fetchMock = mockFetch({
      'GET /environments': {
        body: [
          ENV_ITEM,
          {
            ...ENV_ITEM,
            id: '0f0e0d0c-9999-4c4c-8d8d-0123456789ab',
            name: 'old',
            status: 'disabled',
          },
        ],
      },
      'POST /investigations': { status: 201, body: investigationPending },
      [`POST ${PATH}/start`]: {
        status: 202,
        body: {
          investigation_id: INVESTIGATION_ID,
          turn_id: investigationPending.turns[0]!.id,
          seq: 1,
          status: 'queued',
          events_url: '',
        },
      },
      [`GET ${PATH}`]: { body: investigationPending },
      'GET /investigations': { body: { items: [], next_offset: null } },
    })
    const { router } = renderRoute('/investigations/new')
    // Only active environments are offered; a single one is preselected.
    const select = await screen.findByLabelText('Environment')
    expect(screen.queryByRole('option', { name: /old/ })).not.toBeInTheDocument()
    expect(select).toHaveValue(ENV.id)

    await fill()

    await waitFor(() => expect(router.state.location.pathname).toBe(PATH))
    const [, init] = requestsTo(fetchMock, 'POST', '/investigations')[0]!
    expect(JSON.parse(String(init?.body))).toEqual({
      environment_id: ENV.id,
      title: 'API latency',
      question: 'Why is my API slow?',
      severity: 'high',
    })
    expect(requestsTo(fetchMock, 'POST', `${PATH}/start`)).toHaveLength(1)
  })

  it('opens a created investigation even if starting is refused, and says why', async () => {
    mockFetch({
      'GET /environments': { body: [ENV_ITEM] },
      'POST /investigations': { status: 201, body: investigationPending },
      [`POST ${PATH}/start`]: { status: 503, body: { detail: 'investigation runner is disabled' } },
      [`GET ${PATH}`]: { body: investigationPending },
      'GET /investigations': { body: { items: [], next_offset: null } },
    })
    renderRoute('/investigations/new')
    await fill()
    expect(await screen.findByText('Created, but not started')).toBeInTheDocument()
    expect(screen.getByText('investigation runner is disabled')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /start investigation/i })).toBeInTheDocument()
  })

  it('validates required fields before calling the API', async () => {
    const fetchMock = mockFetch({ 'GET /environments': { body: [ENV_ITEM] } })
    renderRoute('/investigations/new')
    await userEvent.click(await screen.findByRole('button', { name: 'Create and start' }))
    expect(screen.getByRole('alert')).toHaveTextContent(/enter a title and a question/)
    expect(requestsTo(fetchMock, 'POST', '/investigations')).toHaveLength(0)
  })

  it('explains that an environment is needed', async () => {
    mockFetch({ 'GET /environments': { body: [] } })
    renderRoute('/investigations/new')
    expect(await screen.findByText('No active environment')).toBeInTheDocument()
  })
})
