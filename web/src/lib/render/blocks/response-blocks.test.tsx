import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { responseBlock } from '@/lib/api'
import {
  barChartFixture as barChart,
  codeFixture as code,
  cpuLineChart,
  evidenceFixture as evidence,
  markdownFixture as markdown,
  metricFixture as metric,
  presentation1,
  tableFixture as table,
} from '@/test/fixtures'

import { ResponseBlocks } from './response-blocks'

describe('ResponseBlocks', () => {
  it('renders every block type through the registry', async () => {
    render(<ResponseBlocks blocks={presentation1.blocks} onCite={vi.fn()} />)

    expect(screen.getByText(/GET \/slow is the slowest endpoint/)).toBeInTheDocument()
    expect(screen.getByText('{"p95_ms": 1514}')).toBeInTheDocument()
    expect(screen.getByRole('figure', { name: 'Trace totals by operation' })).toBeInTheDocument()
    expect(
      await screen.findByRole('figure', { name: 'p95 latency by operation' }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('figure', { name: 'Trace statistics by operation' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Cited evidence' })).toBeInTheDocument()
  })

  it('shows units, sample counts, windows and sources on metric cards', () => {
    render(<ResponseBlocks blocks={[metric]} onCite={vi.fn()} />)
    const figure = screen.getByRole('figure', { name: 'Trace totals by operation' })
    expect(within(figure).getByText('97 spans')).toBeInTheDocument()
    expect(within(figure).getByText('5.56%')).toBeInTheDocument()
    expect(within(figure).getAllByText('n = 97')).toHaveLength(2)
    expect(within(figure).getByText(/17:16–17:46 UTC/)).toBeInTheDocument()
    expect(within(figure).getByRole('button', { name: 'Evidence ev_1f5967bf' })).toBeInTheDocument()
  })

  it('describes bar charts accessibly and exposes their data with sample counts', async () => {
    render(<ResponseBlocks blocks={[barChart]} />)
    const figure = await screen.findByRole('figure', { name: 'p95 latency by operation' })
    expect(within(figure).getByRole('img')).toHaveAccessibleName(
      /3 operation values; highest request handler - \/slow at 1,535\.3 ms/,
    )
    expect(within(figure).getByText(/fewer than 30 spans/)).toBeInTheDocument()

    await userEvent.click(within(figure).getByRole('button', { name: 'Show data' }))
    const data = within(figure).getByRole('table', { name: /Data for p95 latency/ })
    const rows = within(data)
      .getAllByRole('row')
      .map((r) => r.textContent)
    expect(rows).toEqual([
      'Operationp95 latency (ms)Samples',
      'request handler - /slow1,535.362',
      'GET /slow1,527.131',
      'GET /3.24',
    ])
  })

  it('draws time series from recorded points only, over the full window', async () => {
    render(<ResponseBlocks blocks={[cpuLineChart]} />)
    const figure = await screen.findByRole('figure', { name: 'cpu utilization' })
    expect(within(figure).getByRole('img')).toHaveAccessibleName(
      /3 recorded points from 17:20 to 17:23 UTC; range 1\.1% to 5\.8%, peak at 17:21 UTC/,
    )
    expect(within(figure).getByText(/gaps are not filled/)).toBeInTheDocument()
    await userEvent.click(within(figure).getByRole('button', { name: 'Show data' }))
    const times = within(within(figure).getByRole('table')).getAllByRole('rowheader')
    // 17:22 was not recorded and is not invented.
    expect(times.map((t) => t.textContent)).toEqual(['17:20', '17:21', '17:23'])
  })

  it('sorts tables by column, announcing the order', async () => {
    render(<ResponseBlocks blocks={[table]} />)
    const figure = screen.getByRole('figure', { name: 'Trace statistics by operation' })
    const firstColumn = () =>
      within(figure)
        .getAllByRole('row')
        .slice(1)
        .map((r) => within(r).getAllByRole('cell')[0]!.textContent)
    expect(firstColumn()).toEqual(['request handler - /slow', 'GET /', 'GET /slow'])

    // Numbers sort largest first on the first click, then ascending.
    await userEvent.click(within(figure).getByRole('button', { name: /p95 \(ms\)/ }))
    const header = within(figure).getByRole('columnheader', { name: /p95/ })
    expect(header).toHaveAttribute('aria-sort', 'descending')
    expect(firstColumn()).toEqual(['request handler - /slow', 'GET /slow', 'GET /'])
    await userEvent.click(within(figure).getByRole('button', { name: /p95 \(ms\)/ }))
    expect(header).toHaveAttribute('aria-sort', 'ascending')
    expect(firstColumn()).toEqual(['GET /', 'GET /slow', 'request handler - /slow'])
  })

  it('passes citations from data blocks to the citation handler', async () => {
    const onCite = vi.fn()
    render(<ResponseBlocks blocks={[evidence, markdown]} onCite={onCite} />)
    const cited = screen.getByRole('region', { name: 'Cited evidence' })
    await userEvent.click(within(cited).getByRole('button', { name: 'Evidence ev_1f5967bf' }))
    expect(onCite).toHaveBeenCalledWith('ev_1f5967bf')
  })

  it('replaces malformed, unknown or tampered blocks with a notice and keeps the rest', () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const bad = [
      { type: 'html', html: '<img src=x onerror=alert(1)>' },
      { type: 'component', name: 'Evil', props: {} },
      { ...markdown, onClick: 'alert(1)' },
      { ...metric, items: [{ ...metric.items[0], value: 'NaN' }] },
      { ...barChart, evidence_refs: ['fd_46e29040'] },
      { ...barChart, points: barChart.points.slice(0, 1) },
      { ...cpuLineChart, points: [...cpuLineChart.points].reverse() },
      { ...table, rows: [{ group: 'x', spans: 'many', p95: 1 }] },
      { ...code, language: 'js" onload="x' },
      { ...table, window: { start: '2026-10-21T18:00:00Z', end: '2026-10-21T17:00:00Z' } },
      null,
      'just a string',
    ]
    const { container } = render(<ResponseBlocks blocks={[...bad, markdown]} />)
    expect(screen.getAllByText('This part of the answer could not be displayed.')).toHaveLength(
      bad.length,
    )
    expect(screen.getByText(/GET \/slow is the slowest endpoint/)).toBeInTheDocument()
    expect(container.querySelector('img, script, [onerror], [onload]')).toBeNull()
  })

  it('never lets a __proto__ cell through', () => {
    // As parsed from a response body, where __proto__ is an ordinary key. Zod drops it
    // (the backend rejects it outright), so it can neither render nor pollute prototypes.
    const parsed = responseBlock.parse({
      ...table,
      rows: [JSON.parse('{"group": "x", "__proto__": {"polluted": true}}')],
    })
    expect(parsed.type === 'table' && Object.keys(parsed.rows[0]!)).toEqual(['group'])
    expect(({} as Record<string, unknown>).polluted).toBeUndefined()
  })

  it('renders Markdown in blocks as data, never as markup', () => {
    const { container } = render(
      <ResponseBlocks
        blocks={[{ type: 'markdown', markdown: '<script>window.x=1</script>\n\n**ok** <b>no</b>' }]}
      />,
    )
    expect(container.querySelector('script, b')).toBeNull()
    expect(screen.getByText('ok').tagName).toBe('STRONG')
  })
})
