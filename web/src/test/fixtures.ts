// Shaped like real API responses (src/schemas/investigation.py), including Pydantic's
// timestamp formats, and the answer/report JSON written by main_graph.persist.
export const INVESTIGATION_ID = '89efc27e-1111-4c4c-8d8d-0123456789ab'
export const TURN_1 = '1b2c3d4e-2222-4c4c-8d8d-0123456789ab'
export const TURN_2 = '2b2c3d4e-2222-4c4c-8d8d-0123456789ab'
export const TURN_3 = '3b2c3d4e-2222-4c4c-8d8d-0123456789ab'
export const ASSIGNMENT_1 = '9a8b7c6d-4444-4c4c-8d8d-0123456789ab'
export const ASSIGNMENT_2 = '9a8b7c6d-5555-4c4c-8d8d-0123456789ab'
export const ENV = {
  id: '0f0e0d0c-3333-4c4c-8d8d-0123456789ab',
  name: 'ubuntu-vm',
  type: 'local',
  status: 'active',
} as const

const assignment = (id: string, turnId: string, round: number, status = 'completed') => ({
  id,
  turn_id: turnId,
  round,
  objective: 'Check latency and errors',
  status,
  started_at: '2026-10-21T17:30:02Z',
  completed_at: status === 'completed' ? '2026-10-21T17:31:00Z' : null,
  created_at: '2026-10-21T17:30:00Z',
  updated_at: '2026-10-21T17:31:00Z',
  environment: ENV,
})

export const answer1 = {
  answer_md:
    'GET /slow is the slowest endpoint: p95 ≈ 1,514 ms [ev_1f5967bf, fd_46e29040].\n\n' +
    '| Endpoint | p95 |\n| --- | --- |\n| GET /slow | 1514 ms |\n\n```json\n{"p95_ms": 1514}\n```',
  refs: ['ev_1f5967bf', 'fd_46e29040'],
  fallback: false,
  report_rejected: [],
  rounds: 1,
  limited: false,
  window: null,
  report_version: 1,
  current_report_version: null,
  report_diff: null,
  validation_audit: ['answer: ok'],
  unresolved_problems: ['internal problem text that must never be shown'],
}

export const turn1 = {
  id: TURN_1,
  seq: 1,
  kind: 'initial',
  question: 'Why is my API slow?',
  status: 'completed',
  decision: 'investigated',
  answer_json: answer1,
  error_code: null,
  started_at: '2026-10-21T17:30:01Z',
  completed_at: '2026-10-21T17:31:34Z',
  created_at: '2026-10-21T17:30:00Z',
}

export const turn2Failed = {
  id: TURN_2,
  seq: 2,
  kind: 'follow_up',
  question: 'Is /slow still slow right now?',
  status: 'failed',
  decision: null,
  answer_json: null,
  error_code: 'turn_timeout',
  started_at: '2026-10-21T17:40:01Z',
  completed_at: '2026-10-21T17:50:01Z',
  created_at: '2026-10-21T17:40:00Z',
}

export const turn3Running = {
  id: TURN_3,
  seq: 3,
  kind: 'follow_up',
  question: 'Are there application errors right now?',
  status: 'running',
  decision: null,
  answer_json: null,
  error_code: null,
  started_at: '2026-10-21T17:55:01Z',
  completed_at: null,
  created_at: '2026-10-21T17:55:00Z',
}

const report1 = {
  id: '5e6f7a8b-5555-4c4c-8d8d-0123456789ab',
  turn_id: TURN_1,
  version: 1,
  created_at: '2026-10-21T17:31:34Z',
}

export const investigation = {
  id: INVESTIGATION_ID,
  title: 'API latency on /slow',
  question: 'Why is my API slow?',
  status: 'idle',
  severity: 'high',
  trigger_type: 'manual',
  trigger_json: {},
  current_round: 1,
  started_at: '2026-10-21T17:30:01.123456Z',
  completed_at: null,
  final_result_json: { outcome: 'probable_cause' },
  created_at: '2026-10-21T17:30:00.000001+00:00',
  updated_at: '2026-10-21T17:31:34.5Z',
  assignments: [assignment(ASSIGNMENT_1, TURN_1, 1)],
  turns: [turn1],
  reports: [report1],
}

// Turns deliberately out of order: the UI must sort by seq.
export const investigationRunning = {
  ...investigation,
  status: 'investigating',
  current_round: 2,
  assignments: [
    assignment(ASSIGNMENT_1, TURN_1, 1),
    assignment(ASSIGNMENT_2, TURN_3, 2, 'running'),
  ],
  turns: [turn3Running, turn1, turn2Failed],
}

export const investigationPending = {
  ...investigation,
  status: 'pending',
  started_at: null,
  final_result_json: null,
  assignments: [assignment(ASSIGNMENT_1, TURN_1, 1, 'pending')],
  turns: [
    {
      ...turn1,
      status: 'pending',
      decision: null,
      answer_json: null,
      started_at: null,
      completed_at: null,
    },
  ],
  reports: [],
}

export const listItem = {
  id: INVESTIGATION_ID,
  title: 'API latency on /slow',
  status: 'idle',
  severity: 'high',
  trigger_type: 'manual',
  created_at: '2026-10-21T17:30:00Z',
  updated_at: '2026-10-21T17:31:34Z',
  started_at: '2026-10-21T17:30:01Z',
  completed_at: null,
  environments: [ENV],
  turn_count: 1,
  latest_report_version: 1,
}

export const evidenceCitation = {
  kind: 'evidence',
  ref: 'ev_1f5967bf',
  evidence_type: 'trace',
  title: 'Trace statistics for inquest-demo',
  summary: '2 groups, 62 spans, 0 errors. Slowest: GET /slow p95 1514 ms (31 spans).',
  source_provider: 'signoz',
  tool_name: 'signoz.trace_stats',
  entity_type: 'service',
  entity_identifier: 'inquest-demo',
  observed_at: null,
  collected_at: '2026-10-21T17:30:20Z',
  window_start: '2026-10-21T17:16:00Z',
  window_end: '2026-10-21T17:46:00Z',
  redaction_applied: false,
  environment: ENV,
}

export const findingCitation = {
  kind: 'finding',
  ref: 'fd_46e29040',
  title: '/slow is consistently slow',
  summary: 'p95 ≈ 1.5 s over 31 spans.',
  status: 'confirmed',
  confidence: 0.8,
  created_at: '2026-10-21T17:31:00Z',
  evidence: [
    { ref: 'ev_1f5967bf', stance: 'supports', title: 'Trace statistics for inquest-demo' },
  ],
  environment: ENV,
}

export const reportBody = {
  ...report1,
  report_json: {
    outcome: 'probable_cause',
    summary: 'The /slow handler is slow on every request [ev_1f5967bf].',
    conclusions: [
      {
        key: 'slow-endpoint',
        claim: 'observation',
        statement: 'GET /slow p95 ≈ 1.5 s',
        status: 'confirmed',
        confidence: 'high',
        supporting: ['ev_1f5967bf', 'fd_46e29040'],
        contradicting: [],
      },
    ],
    timeline: [],
    gaps: ['No application logs for inquest-demo'],
    next_checks: ['Profile the /slow handler'],
    changes: [],
  },
}

const WIN = { start: '2026-10-21T17:16:00Z', end: '2026-10-21T17:46:00Z' }

// Shaped like GET /investigations/{id}/turns/{turn_id}/presentation (src/schemas/presentation.py).
export const markdownFixture = {
  type: 'markdown',
  markdown: 'GET /slow is the slowest endpoint [ev_1f5967bf, fd_46e29040].',
}

export const codeFixture = { type: 'code', language: 'json', code: '{"p95_ms": 1514}' }

export const metricFixture = {
  type: 'metric',
  title: 'Trace totals by operation',
  items: [
    {
      label: 'Spans',
      stat: 'count',
      value: 97,
      unit: 'spans',
      sample_count: 97,
      step_seconds: null,
      window: WIN,
      evidence_ref: 'ev_1f5967bf',
    },
    {
      label: 'Error rate',
      stat: 'rate',
      value: 5.56,
      unit: '%',
      sample_count: 97,
      step_seconds: null,
      window: WIN,
      evidence_ref: 'ev_1f5967bf',
    },
  ],
}

export const barChartFixture = {
  type: 'chart',
  kind: 'bar',
  title: 'p95 latency by operation',
  x_label: 'Operation',
  series: [{ key: 'p95', label: 'p95 latency', unit: 'ms' }],
  points: [
    { x: 'request handler - /slow', values: { p95: 1535.3 }, sample_count: 62 },
    { x: 'GET /slow', values: { p95: 1527.1 }, sample_count: 31 },
    { x: 'GET /', values: { p95: 3.2 }, sample_count: 4 },
  ],
  evidence_refs: ['ev_1f5967bf'],
  window: WIN,
  note: '1 group(s) have fewer than 30 spans; their percentiles are small-sample estimates.',
}

export const tableFixture = {
  type: 'table',
  title: 'Trace statistics by operation',
  columns: [
    { key: 'group', label: 'Operation', kind: 'text', unit: null },
    { key: 'spans', label: 'Spans', kind: 'number', unit: 'spans' },
    { key: 'p95', label: 'p95', kind: 'number', unit: 'ms' },
  ],
  rows: [
    { group: 'request handler - /slow', spans: 62, p95: 1535.3 },
    { group: 'GET /', spans: 4, p95: 3.2 },
    { group: 'GET /slow', spans: 31, p95: 1527.1 },
  ],
  evidence_refs: ['ev_1f5967bf'],
  window: WIN,
  note: null,
}

export const evidenceFixture = {
  type: 'evidence',
  items: [
    {
      ref: 'ev_1f5967bf',
      title: 'Trace stats',
      evidence_type: 'trace',
      summary: '2 groups, 62 spans, 0 errors.',
      window: WIN,
    },
  ],
}

export const presentation1 = {
  version: 1,
  turn_id: TURN_1,
  generated_at: '2026-10-25T23:50:00Z',
  blocks: [
    markdownFixture,
    codeFixture,
    metricFixture,
    barChartFixture,
    tableFixture,
    evidenceFixture,
  ],
}

export const cpuLineChart = {
  type: 'chart',
  kind: 'line',
  title: 'cpu utilization',
  x_label: 'Time (UTC)',
  series: [{ key: 's0', label: 'cpu_utilization', unit: 'percent' }],
  points: [
    { x: '2026-10-21T17:20:00Z', values: { s0: 1.2 }, sample_count: null },
    { x: '2026-10-21T17:21:00Z', values: { s0: 5.8 }, sample_count: null },
    { x: '2026-10-21T17:23:00Z', values: { s0: 1.1 }, sample_count: null },
  ],
  evidence_refs: ['ev_a02ae509'],
  window: WIN,
  note: 'Only points recorded with the evidence are drawn; gaps are not filled.',
}
