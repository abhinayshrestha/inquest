import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { MockEventSource } from '@/test/event-source'
import {
  ASSIGNMENT_2,
  ENV,
  evidenceCitation,
  findingCitation,
  INVESTIGATION_ID,
  presentation1,
  TURN_1,
  investigation,
  investigationPending,
  investigationRunning,
  reportBody,
  TURN_3,
  turn2Failed,
  turn3Running,
} from '@/test/fixtures'
import { mockFetch, renderRoute, requestsTo, type Reply } from '@/test/render'

const PATH = `/investigations/${INVESTIGATION_ID}`
const accepted = (seq: number, turnId = TURN_3) => ({
  status: 202,
  body: {
    investigation_id: INVESTIGATION_ID,
    turn_id: turnId,
    seq,
    status: 'queued',
    events_url: `/investigations/${INVESTIGATION_ID}/events`,
  },
})

function backend(detail: Reply | Reply[], extra: Record<string, Reply | Reply[]> = {}) {
  return mockFetch({
    [`GET ${PATH}`]: detail,
    'GET /investigations': { body: { items: [], next_offset: null } },
    [`GET ${PATH}/citations/ev_1f5967bf`]: { body: evidenceCitation },
    [`GET ${PATH}/citations/fd_46e29040`]: { body: findingCitation },
    // Completed turns without their own reply get a server error, so the Markdown
    // fallback is exercised too.
    [`GET ${PATH}/turns/${TURN_1}/presentation`]: { body: presentation1 },
    [`GET ${PATH}/turns/${TURN_3}/presentation`]: { status: 500, body: { detail: 'boom' } },
    ...extra,
  })
}

function turns() {
  return screen.getAllByRole('article')
}

describe('conversation', () => {
  it('shows turns in sequence order with answers, failures and running turns', async () => {
    backend({ body: investigationRunning })
    renderRoute(PATH)
    await screen.findByRole('heading', { level: 1, name: 'API latency on /slow' })

    const [first, second, third] = turns()
    expect(within(first!).getByText('Why is my API slow?')).toBeInTheDocument()
    expect(within(first!).getByRole('table')).toBeInTheDocument()
    expect(within(first!).getByRole('link', { name: /report v1/i })).toHaveAttribute(
      'href',
      `${PATH}/reports/1`,
    )
    // Internal diagnostics in answer_json are never shown.
    expect(screen.queryByText(/internal problem text/)).not.toBeInTheDocument()

    expect(within(second!).getByText('This turn failed')).toBeInTheDocument()
    expect(within(second!).getByText(/ran out of time/)).toBeInTheDocument()
    expect(within(third!).getByText('Are there application errors right now?')).toBeInTheDocument()
    expect(within(third!).getByText(/waiting for the investigator/i)).toBeInTheDocument()
  })

  it('persists across reloads: the conversation comes from the server, not the session', async () => {
    backend({ body: investigationRunning })
    const first = renderRoute(PATH)
    await screen.findByRole('heading', { level: 1, name: 'API latency on /slow' })
    const before = turns().map((t) => t.getAttribute('data-turn-seq'))
    first.unmount()

    renderRoute(PATH)
    await screen.findByRole('heading', { level: 1, name: 'API latency on /slow' })
    expect(turns().map((t) => t.getAttribute('data-turn-seq'))).toEqual(before)
    expect(before).toEqual(['1', '2', '3'])
  })

  it('streams live activity for the running turn with labels, scope and evidence', async () => {
    backend({ body: investigationRunning })
    renderRoute(PATH)
    await screen.findByRole('heading', { level: 1, name: 'API latency on /slow' })
    const source = MockEventSource.latest()
    expect(source.url).toBe(`/api${PATH}/events`)

    const base = { turn_id: TURN_3, assignment_id: ASSIGNMENT_2 }
    source.emit({
      id: 1,
      type: 'turn.decision',
      turn_id: TURN_3,
      payload: { decision: 'investigate', round: 2, objectives: ['Look for ERROR logs'] },
    })
    source.emit({
      id: 2,
      type: 'assignment.started',
      ...base,
      payload: { round: 2, environment_id: ENV.id },
    })
    source.emit({
      id: 3,
      type: 'tool_call.started',
      ...base,
      payload: {
        tool: 'signoz_search_logs',
        call_id: 'c1',
        arguments:
          '{"time_range":{"start":"2026-10-21T17:40:00Z","end":"2026-10-21T17:55:00Z"},"service":"inquest-demo","body_contains":"token=abc123"}',
      },
    })
    source.emit({
      id: 4,
      type: 'tool_call.finished',
      ...base,
      payload: {
        tool: 'signoz_search_logs',
        call_id: 'c1',
        status: 'succeeded',
        outcome: 'observations',
        telemetry_available: 'yes',
        evidence: ['ev_1f5967bf'],
        replayed: false,
      },
    })
    source.emit({
      id: 5,
      type: 'hypothesis.recorded',
      ...base,
      payload: { ref: 'hy_0a1b2c3d', statement: '<script>alert(1)</script> errors from /error' },
    })

    const activity = await screen.findByRole('list', { name: 'Investigation activity' })
    expect(within(activity).getByText('Gathering new evidence')).toBeInTheDocument()
    expect(within(activity).getByText('Look for ERROR logs')).toBeInTheDocument()
    expect(within(activity).getByText('Investigating ubuntu-vm')).toBeInTheDocument()
    expect(within(activity).getByText('Search logs')).toBeInTheDocument()
    expect(within(activity).getByText(/Observed/)).toBeInTheDocument()
    expect(within(activity).getByText('inquest-demo')).toBeInTheDocument()
    expect(within(activity).getByText('17:40–17:55 UTC')).toBeInTheDocument()
    expect(
      within(activity).getByRole('button', { name: 'Evidence ev_1f5967bf' }),
    ).toBeInTheDocument()
    // Free-text filters and markup in recorded text never become live content.
    expect(activity).not.toHaveTextContent('abc123')
    expect(activity.querySelector('script')).toBeNull()
    expect(
      within(activity).getByText(/<script>alert\(1\)<\/script> errors from \/error/),
    ).toBeInTheDocument()
  })

  it('refetches the investigation on terminal events and shows the new answer', async () => {
    const done = {
      ...investigationRunning,
      status: 'idle',
      turns: investigationRunning.turns.map((t) =>
        t.id === TURN_3
          ? {
              ...t,
              status: 'completed',
              decision: 'reused_evidence',
              completed_at: '2026-10-21T17:56:00Z',
              answer_json: {
                answer_md: 'Two ERROR logs at 17:02 [ev_1f5967bf].',
                refs: ['ev_1f5967bf'],
              },
            }
          : t,
      ),
    }
    const fetchMock = backend([{ body: investigationRunning }, { body: done }])
    renderRoute(PATH)
    await screen.findByRole('heading', { level: 1, name: 'API latency on /slow' })

    MockEventSource.latest().emit({
      id: 9,
      type: 'turn.completed',
      turn_id: TURN_3,
      payload: { decision: 'reused_evidence' },
    })

    expect(await screen.findByText(/Two ERROR logs at 17:02/)).toBeInTheDocument()
    expect(requestsTo(fetchMock, 'GET', PATH).length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText('Answered from existing evidence')).toBeInTheDocument()
    // Earlier turns stay as they were.
    expect(screen.getByText('This turn failed')).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Follow-up question' })).toBeEnabled()
  })

  it('submits a follow-up to an idle investigation', async () => {
    const running = {
      ...investigation,
      status: 'correlating',
      turns: [...investigation.turns, { ...turn3Running, seq: 2, status: 'pending' }],
    }
    const fetchMock = backend([{ body: investigation }, { body: running }], {
      [`POST ${PATH}/turns`]: accepted(2),
    })
    renderRoute(PATH)
    const input = await screen.findByRole('textbox', { name: 'Follow-up question' })
    const user = userEvent.setup()

    await user.type(input, '  Are there errors right now?  ')
    await user.click(screen.getByRole('button', { name: 'Send follow-up' }))

    await waitFor(() => expect(requestsTo(fetchMock, 'POST', `${PATH}/turns`)).toHaveLength(1))
    const [, init] = requestsTo(fetchMock, 'POST', `${PATH}/turns`)[0]!
    expect(JSON.parse(String(init?.body))).toEqual({ question: 'Are there errors right now?' })
    await waitFor(() => expect(input).toHaveValue(''))
    // The new turn appears and the composer is locked while it runs.
    expect(await screen.findByText('Are there application errors right now?')).toBeInTheDocument()
    expect(input).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Cancel the running turn' })).toBeInTheDocument()
  })

  it('keeps the question and explains a refused follow-up', async () => {
    backend(
      { body: investigation },
      {
        [`POST ${PATH}/turns`]: { status: 409, body: { detail: 'a turn is already active' } },
      },
    )
    renderRoute(PATH)
    const input = await screen.findByRole('textbox', { name: 'Follow-up question' })
    await userEvent.type(input, 'Still slow?')
    await userEvent.click(screen.getByRole('button', { name: 'Send follow-up' }))
    expect(await screen.findByText('a turn is already active')).toBeInTheDocument()
    expect(input).toHaveValue('Still slow?')
  })

  it('cancels the running turn', async () => {
    const cancelled = {
      ...investigationRunning,
      status: 'idle',
      turns: investigationRunning.turns.map((t) =>
        t.id === TURN_3 ? { ...t, status: 'cancelled', error_code: 'cancelled' } : t,
      ),
    }
    const fetchMock = backend([{ body: investigationRunning }, { body: cancelled }], {
      [`POST ${PATH}/cancel`]: {
        body: { investigation_id: INVESTIGATION_ID, turn_id: TURN_3, status: 'cancelled' },
      },
    })
    renderRoute(PATH)
    await userEvent.click(await screen.findByRole('button', { name: 'Cancel the running turn' }))

    expect(await screen.findByText('Cancelled')).toBeInTheDocument()
    expect(requestsTo(fetchMock, 'POST', `${PATH}/cancel`)).toHaveLength(1)
    // Previous answers survive the cancellation.
    expect(screen.getByRole('table')).toBeInTheDocument()
  })

  it('starts a created investigation', async () => {
    const started = {
      ...investigationPending,
      status: 'correlating',
      turns: [{ ...investigationPending.turns[0]!, status: 'running' }],
    }
    const fetchMock = backend([{ body: investigationPending }, { body: started }], {
      [`POST ${PATH}/start`]: accepted(1),
    })
    renderRoute(PATH)
    expect(await screen.findByText('Not started yet.')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /start investigation/i }))
    await waitFor(() => expect(requestsTo(fetchMock, 'POST', `${PATH}/start`)).toHaveLength(1))
    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: /start investigation/i }),
      ).not.toBeInTheDocument(),
    )
  })

  it('explains a start refused because the runner is disabled', async () => {
    backend(
      { body: investigationPending },
      {
        [`POST ${PATH}/start`]: {
          status: 503,
          body: { detail: 'investigation runner is disabled' },
        },
      },
    )
    renderRoute(PATH)
    await userEvent.click(await screen.findByRole('button', { name: /start investigation/i }))
    expect(await screen.findByText('Could not start')).toBeInTheDocument()
    expect(screen.getByText('investigation runner is disabled')).toBeInTheDocument()
  })

  it('opens evidence and finding citations from an answer and navigates between them', async () => {
    const fetchMock = backend({ body: investigation })
    const { router } = renderRoute(PATH)
    // Wait for the stored response blocks to replace the Markdown fallback.
    await screen.findByText('Trace totals by operation')
    await userEvent.click(screen.getByRole('button', { name: 'Finding fd_46e29040' }))

    const sheet = await screen.findByRole('dialog')
    expect(router.state.location.search).toBe('?ref=fd_46e29040')
    expect(await within(sheet).findByText('/slow is consistently slow')).toBeInTheDocument()

    await userEvent.click(within(sheet).getByRole('button', { name: 'Evidence ev_1f5967bf' }))
    expect(await within(sheet).findByText(/62 spans, 0 errors/)).toBeInTheDocument()
    expect(within(sheet).getByText('Trace statistics')).toBeInTheDocument()
    expect(router.state.location.search).toBe('?ref=ev_1f5967bf')
    expect(requestsTo(fetchMock, 'GET', `${PATH}/citations/ev_1f5967bf`)).toHaveLength(1)

    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(router.state.location.search).toBe('')
  })

  it('restores an open citation from the URL and handles unknown refs', async () => {
    backend(
      { body: investigation },
      {
        [`GET ${PATH}/citations/ev_00000000`]: {
          status: 404,
          body: { detail: 'citation not found' },
        },
      },
    )
    renderRoute(`${PATH}?ref=ev_00000000`)
    const sheet = await screen.findByRole('dialog')
    expect(await within(sheet).findByText(/does not resolve/)).toBeInTheDocument()
  })

  it('shows not found for a 404 and never requests impossible ids', async () => {
    const fetchMock = backend({ status: 404, body: { detail: 'investigation not found' } })
    renderRoute(PATH)
    expect(await screen.findByText('Investigation not found')).toBeInTheDocument()
    expect(MockEventSource.instances).toHaveLength(0)

    renderRoute('/investigations/not-a-uuid')
    await waitFor(() => expect(screen.getAllByText('Investigation not found')).toHaveLength(2))
    expect(requestsTo(fetchMock, 'GET', '/investigations/not-a-uuid')).toHaveLength(0)
  })

  it('shows a loading skeleton, then a retryable error', async () => {
    mockFetch({})
    renderRoute(PATH)
    // Session and organization first, then the investigation itself.
    expect(screen.getByRole('status')).toBeInTheDocument()
    expect(await screen.findByText('Backend unreachable')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument()
  })
})

describe('response blocks', () => {
  const done = {
    ...investigationRunning,
    status: 'idle',
    turns: investigationRunning.turns.map((t) =>
      t.id === TURN_3
        ? { ...t, status: 'completed', decision: 'reused_evidence', completed_at: '2026-10-21T17:56:00Z',
            answer_json: { answer_md: 'Two **ERROR** logs at 17:02 [ev_1f5967bf].' } }
        : t,
    ),
  } // prettier-ignore

  it('renders stored blocks per answer and falls back to the validated Markdown', async () => {
    const fetchMock = backend({ body: done })
    renderRoute(PATH)
    const first = (await screen.findAllByRole('article'))[0]!
    expect(
      await within(first).findByRole('figure', { name: 'p95 latency by operation' }),
    ).toBeInTheDocument()
    expect(
      within(first).getByRole('figure', { name: 'Trace statistics by operation' }),
    ).toBeInTheDocument()

    // Turn 3's blocks fail to load (500): its answer is still shown, as Markdown.
    const third = screen.getAllByRole('article')[2]!
    await waitFor(() =>
      expect(within(third).queryByText('Preparing evidence views…')).not.toBeInTheDocument(),
    )
    expect(within(third).getByText('ERROR').tagName).toBe('STRONG')
    expect(within(third).queryByRole('figure')).not.toBeInTheDocument()
    // Failed turn 2 never asks for blocks.
    expect(
      requestsTo(fetchMock, 'GET', `${PATH}/turns/${turn2Failed.id}/presentation`),
    ).toHaveLength(0)
  })

  it('falls back to Markdown for an unknown presentation version', async () => {
    backend(
      { body: investigation },
      {
        [`GET ${PATH}/turns/${TURN_1}/presentation`]: { body: { ...presentation1, version: 2 } },
      },
    )
    renderRoute(PATH)
    expect(await screen.findByRole('table')).toBeInTheDocument() // the Markdown table
    await waitFor(() =>
      expect(screen.queryByText('Preparing evidence views…')).not.toBeInTheDocument(),
    )
    expect(screen.queryByRole('figure')).not.toBeInTheDocument()
  })

  it('reopening an answer reuses the stored blocks instead of requesting them again', async () => {
    const fetchMock = backend(
      { body: investigation },
      { [`GET ${PATH}/reports/1`]: { body: reportBody } },
    )
    const user = userEvent.setup()
    const { router } = renderRoute(PATH)
    await screen.findByRole('figure', { name: 'p95 latency by operation' })

    await user.click(screen.getByRole('link', { name: /report v1/i }))
    await screen.findByRole('heading', { level: 1, name: 'Report v1' })
    await router.navigate(-1)
    // The report page shows the same chart (reused evidence views); wait for the conversation.
    await screen.findByRole('heading', { level: 1, name: 'API latency on /slow' })
    expect(
      await screen.findByRole('figure', { name: 'p95 latency by operation' }),
    ).toBeInTheDocument()
    expect(requestsTo(fetchMock, 'GET', `${PATH}/turns/${TURN_1}/presentation`)).toHaveLength(1)
  })
})

describe('report version page', () => {
  it('renders the basic report with citations', async () => {
    backend({ body: investigation }, { [`GET ${PATH}/reports/1`]: { body: reportBody } })
    renderRoute(`${PATH}/reports/1`)
    expect(await screen.findByRole('heading', { level: 1, name: 'Report v1' })).toBeInTheDocument()
    expect(screen.getByText('Probable cause')).toBeInTheDocument()
    expect(screen.getByText('GET /slow p95 ≈ 1.5 s')).toBeInTheDocument()
    expect(screen.getByText('Profile the /slow handler')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Evidence ev_1f5967bf' }).length).toBe(2)
  })

  it('shows not found for a missing version', async () => {
    backend(
      { body: investigation },
      {
        [`GET ${PATH}/reports/9`]: { status: 404, body: { detail: 'report version not found' } },
      },
    )
    renderRoute(`${PATH}/reports/9`)
    expect(await screen.findByText('Report not found')).toBeInTheDocument()
  })
})
