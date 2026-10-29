import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  ENV,
  ENV_ITEM,
  evidenceCitation,
  INVESTIGATION_ID,
  investigation,
  presentation1,
  reportListItem,
  TURN_1,
  TURN_2,
  ORGANIZATION_ID,
} from '@/test/fixtures'
import { mockFetch, renderRoute, requestsTo, type Reply } from '@/test/render'

const BASE = `/investigations/${INVESTIGATION_ID}`
const ref = (r: string) => ({
  id: '1f5967bf-b673-4b85-bb97-451cbd8e4b7b',
  ref: r,
  kind: r.startsWith('ev') ? 'evidence' : 'finding',
})

// Stored formats as written by main_validation.merge_report / report_diff (live data shape).
const v1 = {
  outcome: 'root_cause_identified',
  summary: 'Three groups exceed 500 ms p95 [ev_1f5967bf].',
  conclusions: [
    { key: 'slow-endpoints', as_of: '2026-10-21T17:46+00:00', claim: 'observation', scope: 'current', status: 'confirmed', statement: 'GET /slow p95 1527 ms over 28 spans.', confidence: 'high', supporting: [ref('ev_1f5967bf')], contradicting: [] },
    { key: 'cpu-low', as_of: '2026-10-21T17:46+00:00', claim: 'observation', scope: 'current', status: 'confirmed', statement: 'Host CPU averaged 1.4 %.', confidence: 'high', supporting: [ref('ev_a02ae509')], contradicting: [] },
  ],
  history: [],
  timeline: [{ at: '17:16–17:46 UTC', event: 'Trace window: 504 spans.', refs: [ref('ev_1f5967bf')] }],
  gaps: ['No application logs for inquest-demo'],
  next_checks: ['Profile the /slow handler'],
  changes: [],
  diff: { added: [], outcome: [null, 'root_cause_identified'], material: true, retracted: [], status_changed: [], no_longer_current: [] },
} // prettier-ignore

const v2 = {
  outcome: 'inconclusive',
  summary: 'Errors on /error are intentional.',
  conclusions: [
    { key: 'error-endpoint', as_of: '2026-10-21T17:48+00:00', claim: 'causal', scope: 'current', status: 'unverified', statement: '/error returns HTTP 500 by design.', confidence: 'low', supporting: [ref('ev_2c0a209e')], contradicting: [ref('fd_46e29040')] },
    { key: 'errors-with-latency', as_of: '2026-10-21T17:48+00:00', claim: 'temporal_overlap', scope: 'current', status: 'probable', statement: 'Errors and slow requests occur in the same window.', confidence: 'medium', supporting: [ref('ev_1f5967bf')], contradicting: [] },
  ],
  history: [
    { key: 'slow-endpoints', as_of: '2026-10-21T17:46+00:00', scope: 'historical', status: 'confirmed', statement: 'GET /slow p95 1527 ms over 28 spans.', supporting: [ref('ev_1f5967bf')], from_version: 1 },
    { key: 'cpu-low', as_of: '2026-10-21T17:46+00:00', scope: 'historical', status: 'retracted', statement: 'Host CPU averaged 1.4 %.', supporting: [], from_version: 1 },
  ],
  timeline: [],
  gaps: [],
  next_checks: [],
  changes: [{ change: 'retracted', key: 'cpu-low', statement: 'Host CPU averaged 1.4 %.', reason: 'The metric was for another host.', refs: [ref('ev_a02ae509')] }],
  diff: { added: ['error-endpoint'], outcome: ['root_cause_identified', 'inconclusive'], material: true, retracted: ['cpu-low'], status_changed: [['slow-endpoints', 'confirmed', 'probable']], no_longer_current: ['slow-endpoints'] },
} // prettier-ignore

// Written before claims, keyed diffs and stated changes existed.
const legacy = {
  outcome: 'probable_cause',
  summary: 'Legacy summary.',
  conclusions: [{ statement: 'GET /slow is slow.', status: 'probable', confidence: 'medium', supporting: ['ev_1f5967bf'], contradicting: [] }],
  gaps: [],
  next_checks: [],
  changes: null,
  diff: { added: ['errors coincide with latency'], outcome: ['probable_cause', 'probable_cause'], removed: ['old statement about /error'], material: true, status_changed: [] },
} // prettier-ignore

const reportRow = (version: number, turnId: string, body: unknown) => ({
  id: `5e6f7a8b-555${version}-4c4c-8d8d-0123456789ab`,
  turn_id: turnId,
  version,
  created_at: `2026-10-21T17:5${version}:00Z`,
  report_json: body,
})

const withTwoVersions = {
  ...investigation,
  reports: [
    { id: '5e6f7a8b-5551-4c4c-8d8d-0123456789ab', turn_id: TURN_1, version: 1, created_at: '2026-10-21T17:51:00Z' },
    { id: '5e6f7a8b-5552-4c4c-8d8d-0123456789ab', turn_id: TURN_2, version: 2, created_at: '2026-10-21T17:52:00Z' },
  ],
} // prettier-ignore

function backend(extra: Record<string, Reply | Reply[]> = {}) {
  return mockFetch({
    [`GET ${BASE}`]: { body: withTwoVersions },
    [`GET ${BASE}/reports/1`]: { body: reportRow(1, TURN_1, v1) },
    [`GET ${BASE}/reports/2`]: { body: reportRow(2, TURN_2, v2) },
    [`GET ${BASE}/turns/${TURN_1}/presentation`]: { body: presentation1 },
    [`GET ${BASE}/turns/${TURN_2}/presentation`]: { status: 409, body: { detail: 'no answer' } },
    [`GET ${BASE}/citations/ev_1f5967bf`]: { body: evidenceCitation },
    'GET /environments': { body: [ENV_ITEM] },
    'GET /reports': { body: { items: [reportListItem], next_offset: null } },
    ...extra,
  })
}

describe('reports explorer', () => {
  it('lists the latest version per investigation by default', async () => {
    const fetchMock = backend()
    renderRoute('/reports')
    const list = await screen.findByRole('list', { name: 'Reports' })
    const row = within(list).getByRole('link', { name: /API latency on \/slow/ })
    expect(row).toHaveAttribute('href', `${BASE}/reports/2`)
    expect(row).toHaveTextContent('v2 · follow-up 1 · 3 conclusions · ubuntu-vm')
    expect(within(row).getByText('Inconclusive')).toBeInTheDocument()
    const [input] = requestsTo(fetchMock, 'GET', '/reports')[0]!
    expect(new URL(String(input), 'http://x').searchParams.get('latest_only')).toBe('true')
  })

  it('filters by outcome, environment, versions and title, keeping filters in the URL', async () => {
    const fetchMock = backend()
    const { router } = renderRoute('/reports')
    await screen.findByRole('list', { name: 'Reports' })
    const user = userEvent.setup()

    await user.selectOptions(screen.getByLabelText('Outcome'), 'probable_cause')
    await user.selectOptions(screen.getByLabelText('Environment'), ENV.id)
    await user.click(screen.getByLabelText('Include earlier versions'))
    await user.type(screen.getByLabelText('Investigation title'), '  slow  ')
    await user.click(screen.getByRole('button', { name: 'Search reports' }))

    await waitFor(() => {
      const last = requestsTo(fetchMock, 'GET', '/reports').at(-1)!
      const params = new URL(String(last[0]), 'http://x').searchParams
      expect(Object.fromEntries(params)).toEqual({
        limit: '20', offset: '0', organization_id: ORGANIZATION_ID, outcome: 'probable_cause',
        environment_id: ENV.id, q: 'slow',
      }) // prettier-ignore
    })
    expect(router.state.location.search).toContain('outcome=probable_cause')
    expect(router.state.location.search).toContain('versions=all')
  })

  it('loads further pages without refetching earlier ones', async () => {
    const second = {
      ...reportListItem,
      id: '00000000-0000-4000-8000-000000000009',
      investigation_title: 'Older report',
      version: 1,
    }
    const fetchMock = backend({
      'GET /reports?limit=20&offset=0&latest_only=true': { body: { items: [reportListItem], next_offset: 20 } },
      'GET /reports?limit=20&offset=20&latest_only=true': { body: { items: [second], next_offset: null } },
    }) // prettier-ignore
    renderRoute('/reports')
    await userEvent.click(await screen.findByRole('button', { name: 'Load more' }))
    expect(await screen.findByText('Older report')).toBeInTheDocument()
    expect(requestsTo(fetchMock, 'GET', '/reports')).toHaveLength(2)
  })

  it('explains empty results and offers to clear filters', async () => {
    backend({ 'GET /reports': { body: { items: [], next_offset: null } } })
    renderRoute('/reports?outcome=inconclusive')
    expect(await screen.findByText('No reports match these filters')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Clear filters' })).toHaveAttribute('href', '/reports')
  })

  it('retries a failed listing', async () => {
    backend({
      'GET /reports': [
        { status: 503, body: { detail: 'database unavailable' } },
        { body: { items: [reportListItem], next_offset: null } },
      ],
    })
    renderRoute('/reports')
    await userEvent.click(await screen.findByRole('button', { name: /try again/i }))
    expect(await screen.findByRole('list', { name: 'Reports' })).toBeInTheDocument()
  })
})

describe('report view', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('shows outcome, context and that the report is a record, not live telemetry', async () => {
    // The age shown is relative to now: pin now, 15 hours after the observation (only
    // Date is faked, so the queries' own timers keep running).
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-10-22T08:48:00Z'))
    backend()
    renderRoute(`${BASE}/reports/2`)
    expect(await screen.findByRole('heading', { level: 1, name: 'Report v2' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'API latency on /slow' })).toHaveAttribute('href', BASE)
    // Outcome badge, plus the outcome change line further down.
    expect(screen.getAllByText('Inconclusive')).toHaveLength(2)
    expect(screen.getByText('Latest version')).toBeInTheDocument()
    expect(screen.getByText(/A record, not live telemetry/)).toHaveTextContent(
      /observed up to Oct 21, 17:48 UTC \(15 hours ago\)/,
    )
  })

  it('separates causal, overlap and observation claims and dates every conclusion', async () => {
    backend()
    renderRoute(`${BASE}/reports/2`)
    const causal = await screen.findByRole('region', { name: /^Causal/ })
    expect(within(causal).getByText(/Only accepted with trace evidence/)).toBeInTheDocument()
    expect(within(causal).getByText('/error returns HTTP 500 by design.')).toBeInTheDocument()
    expect(within(causal).getByText(/Observed up to Oct 21, 17:48 UTC/)).toBeInTheDocument()
    const overlap = screen.getByRole('region', { name: /^Overlap in time/ })
    expect(
      within(overlap).getByText(/Happening together is not a relationship/),
    ).toBeInTheDocument()
    expect(within(causal).getByRole('button', { name: 'Finding fd_46e29040' })).toBeInTheDocument()
  })

  it('keeps earlier conclusions as history, marking retractions', async () => {
    backend()
    renderRoute(`${BASE}/reports/2`)
    const history = await screen.findByRole('list', { name: 'Historical conclusions' })
    const items = within(history).getAllByRole('listitem')
    expect(items[0]).toHaveTextContent('From version 1')
    expect(within(items[1]!).getByText('Host CPU averaged 1.4 %.')).toHaveClass('line-through')
    expect(within(items[1]!).getByText('Retracted')).toBeInTheDocument()
  })

  it('explains what changed from the previous version', async () => {
    backend()
    renderRoute(`${BASE}/reports/2`)
    const changes = await screen.findByRole('list', { name: 'Recorded changes' })
    expect(screen.getByText(/Outcome:/).parentElement).toHaveTextContent('Root cause identified')
    const text = within(changes)
      .getAllByRole('listitem')
      .map((li) => li.textContent)
    expect(text).toEqual([
      'Added/error returns HTTP 500 by design.',
      'Status changedGET /slow p95 1527 ms over 28 spans.ConfirmedProbable',
      'RetractedHost CPU averaged 1.4 %.',
      'No longer current (kept in history)GET /slow p95 1527 ms over 28 spans.',
    ])
    expect(screen.getByText('Why: The metric was for another host.')).toBeInTheDocument()
  })

  it('navigates between versions', async () => {
    backend()
    const { router } = renderRoute(`${BASE}/reports/2`)
    await screen.findByRole('heading', { level: 1, name: 'Report v2' })
    const nav = screen.getAllByRole('navigation', { name: 'Report versions' })[0]!
    expect(within(nav).getByRole('link', { name: /v2/ })).toHaveAttribute('aria-current', 'page')

    await userEvent.click(within(nav).getByRole('link', { name: 'Previous version' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Report v1' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe(`${BASE}/reports/1`)
    expect(screen.getByText('Superseded by v2')).toBeInTheDocument()
    expect(screen.getByText('First version of this report.')).toBeInTheDocument()
    expect(screen.getByText('Trace window: 504 spans.')).toBeInTheDocument()
    expect(screen.getByText('Profile the /slow handler')).toBeInTheDocument()
    // Charts from the answer that produced v1 are reused from its stored blocks.
    expect(
      await screen.findByRole('figure', { name: 'p95 latency by operation' }),
    ).toBeInTheDocument()
  })

  it('opens evidence citations from conclusions', async () => {
    backend()
    const { router } = renderRoute(`${BASE}/reports/1`)
    const observations = await screen.findByRole('region', { name: /^Observation/ })
    await userEvent.click(
      within(observations).getAllByRole('button', { name: 'Evidence ev_1f5967bf' })[0]!,
    )
    const sheet = await screen.findByRole('dialog')
    expect(await within(sheet).findByText(/62 spans, 0 errors/)).toBeInTheDocument()
    expect(router.state.location.search).toBe('?ref=ev_1f5967bf')
  })

  it('renders reports written before claims and keyed diffs', async () => {
    backend({ [`GET ${BASE}/reports/2`]: { body: reportRow(2, TURN_2, legacy) } })
    renderRoute(`${BASE}/reports/2`)
    const unclassified = await screen.findByRole('region', { name: /^Not classified/ })
    expect(within(unclassified).getByText('GET /slow is slow.')).toBeInTheDocument()
    expect(
      within(unclassified).getByRole('button', { name: 'Evidence ev_1f5967bf' }),
    ).toBeInTheDocument()
    expect(screen.getByText('old statement about /error')).toBeInTheDocument()
    expect(screen.getByText(/Observation time not recorded/)).toBeInTheDocument()
  })

  it('shows not found, malformed content and retryable errors', async () => {
    backend({
      [`GET ${BASE}/reports/9`]: { status: 404, body: { detail: 'report version not found' } },
      [`GET ${BASE}/reports/3`]: { body: reportRow(3, TURN_2, { summary: 'no outcome' }) },
    })
    const first = renderRoute(`${BASE}/reports/9`)
    expect(await screen.findByText('Report not found')).toBeInTheDocument()
    first.unmount()
    renderRoute(`${BASE}/reports/3`)
    expect(await screen.findByText('This report could not be displayed')).toBeInTheDocument()
  })
})
