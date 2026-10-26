import { describe, expect, it } from 'vitest'

import { reflowMarkdown } from './reflow'

// Verbatim shape of a stored live answer (investigation 89efc27e, turn 1).
const LIVE =
  '## Investigation Summary: API Latency and Errors in `inquest-demo` (Last 30 Minutes, 17:16–17:46 UTC)  ' +
  '[ev_1f5967bf, ev_00c5ed87]  ### Slow Endpoints Identified  Three operation groups exceed the 500 ms p95 threshold:  ' +
  '- **Request handler for `/slow`** – 56 spans, p95 ≈ 1,535 ms  - **GET `/slow`** – 28 spans  ' +
  'All other operations are fast.  ### Key Takeaway  The latency issue is isolated to `/slow`.'

describe('reflowMarkdown', () => {
  it('restores headings, lists and paragraphs glued together with spaces', () => {
    expect(reflowMarkdown(LIVE).split('\n\n')).toEqual([
      '## Investigation Summary: API Latency and Errors in `inquest-demo` (Last 30 Minutes, 17:16–17:46 UTC)',
      '[ev_1f5967bf, ev_00c5ed87]',
      '### Slow Endpoints Identified',
      'Three operation groups exceed the 500 ms p95 threshold:',
      '- **Request handler for `/slow`** – 56 spans, p95 ≈ 1,535 ms',
      '- **GET `/slow`** – 28 spans',
      'All other operations are fast.',
      '### Key Takeaway',
      'The latency issue is isolated to `/slow`.',
    ])
  })

  it('turns • bullets into Markdown list items', () => {
    expect(reflowMarkdown('Errors right now:  • Two ERROR logs  • Trace stats')).toBe(
      'Errors right now:\n\n- Two ERROR logs\n\n- Trace stats',
    )
  })

  it('leaves well-formed Markdown, plain prose and code untouched', () => {
    const normal = '## Title\n\nText with a hard break  \nnext line.\n\n- a\n- b'
    const prose = 'GET /slow was slowest.  Across the window it stayed slow.'
    const code = 'Config:\n\n```yaml\nkeys:  - a\n  - b\n```'
    expect(reflowMarkdown(normal)).toBe(normal)
    expect(reflowMarkdown(prose)).toBe(prose)
    expect(reflowMarkdown(code)).toBe(code)
  })

  it('reflows only the affected line, keeping appended Inquest notes', () => {
    const md = 'Summary  ### Details  Body text.\n\n[Inquest] Not established by evidence.'
    expect(reflowMarkdown(md)).toBe(
      'Summary\n\n### Details\n\nBody text.\n\n[Inquest] Not established by evidence.',
    )
  })
})
