import { describe, expect, it } from 'vitest'

import type { ProgressEvent } from '@/lib/api'
import { ASSIGNMENT_2, ENV, TURN_1, TURN_3 } from '@/test/fixtures'

import { activitySummary, buildActivity, toolScope, type AssignmentStep } from './activity'

let nextId = 1
function event(
  type: string,
  payload: Record<string, unknown> = {},
  extra: Partial<ProgressEvent> = {},
) {
  return {
    id: nextId++,
    type,
    turn_id: TURN_3,
    assignment_id: ASSIGNMENT_2,
    created_at: `2026-10-21T17:55:${String(nextId).padStart(2, '0')}Z`,
    payload,
    ...extra,
  } satisfies ProgressEvent
}

const ARGS =
  '{"time_range":{"start":"2026-10-21T17:40:00Z","end":"2026-10-21T17:55:00Z"},' +
  '"service":"inquest-demo","identity":"service","severity":["ERROR"],' +
  '"body_contains":"password=hunter2","where":[{"key":"user.email","op":"=","value":"a@b.c"}]}'

describe('buildActivity', () => {
  it('groups events per turn and pairs tool calls by call id', () => {
    const turns = buildActivity([
      event('turn.queued', {}, { assignment_id: null }),
      event(
        'turn.decision',
        { decision: 'investigate', round: 2, objectives: ['Check errors'] },
        { assignment_id: null },
      ),
      event('assignment.started', { round: 2, environment_id: ENV.id, objective: 'Check errors' }),
      event('tool_call.started', { tool: 'signoz_search_logs', call_id: 'c1', arguments: ARGS }),
      event('tool_call.finished', {
        tool: 'signoz_search_logs',
        call_id: 'c1',
        status: 'succeeded',
        outcome: 'observations',
        telemetry_available: 'yes',
        evidence: ['ev_aaaaaaaa'],
        error_code: null,
        replayed: false,
      }),
      event('hypothesis.recorded', { ref: 'hy_bbbbbbbb', statement: 'Errors come from /error' }),
      event('hypothesis.updated', { ref: 'hy_bbbbbbbb', status: 'supported' }),
      event('finding.recorded', { ref: 'fd_cccccccc', status: 'confirmed', title: '2 ERROR logs' }),
      event('assignment.finished', { status: 'completed' }),
      event('turn.started', {}, { turn_id: TURN_1, assignment_id: null }),
    ])

    expect([...turns.keys()]).toEqual([TURN_3, TURN_1])
    const turn = turns.get(TURN_3)!
    expect(turn.queued).toBe(true)
    expect(turn.steps.map((s) => s.kind)).toEqual(['notice', 'decision', 'assignment'])
    const assignment = turn.steps[2] as AssignmentStep
    expect(assignment).toMatchObject({ round: 2, environmentId: ENV.id, status: 'completed' })
    expect(assignment.items).toHaveLength(3)
    expect(assignment.items[0]).toMatchObject({
      kind: 'tool',
      status: 'succeeded',
      evidence: ['ev_aaaaaaaa'],
      telemetryAvailable: 'yes',
    })
    expect(assignment.items[1]).toMatchObject({
      statement: 'Errors come from /error',
      status: 'supported',
    })
    expect(activitySummary(turn)).toBe('1 tool call · 1 evidence · 1 finding')
  })

  it('does not duplicate operations replayed after a crash (new ids, same call)', () => {
    const started = { tool: 'signoz_trace_stats', call_id: 'c9', arguments: '{}' }
    const finished = { ...started, status: 'succeeded', outcome: 'empty', replayed: true }
    const turn = buildActivity([
      event('tool_call.started', started),
      event('tool_call.started', started),
      event('tool_call.finished', finished),
      event('finding.recorded', { ref: 'fd_dddddddd', title: 'x', status: 'inconclusive' }),
      event('finding.recorded', { ref: 'fd_dddddddd', title: 'x', status: 'inconclusive' }),
    ]).get(TURN_3)!
    const items = (turn.steps[0] as AssignmentStep).items
    expect(items.map((i) => i.key)).toEqual(['c9', 'fd_dddddddd'])
    expect(items[0]).toMatchObject({ status: 'succeeded', outcome: 'empty', replayed: true })
  })

  it('skips malformed payloads and unknown statuses instead of rendering them', () => {
    const turn = buildActivity([
      event('tool_call.started', { tool: 'signoz_trace_stats' }), // no call id
      event('turn.decision', { decision: '<img src=x onerror=alert(1)>' }),
      event('tool_call.started', { tool: 'signoz_get_trace', call_id: 'c2', arguments: 42 }),
      event('tool_call.finished', {
        call_id: 'c2',
        status: 'exploded',
        evidence: ['ev_eeeeeeee', 'not-a-ref', 7],
      }),
      event('turn.completed', {}, { turn_id: null }),
    ]).get(TURN_3)!
    expect(turn.steps).toHaveLength(1)
    const [item] = (turn.steps[0] as AssignmentStep).items
    expect(item).toMatchObject({ status: 'unknown', scope: [], evidence: ['ev_eeeeeeee'] })
  })
})

describe('toolScope', () => {
  it('shows only allowlisted scope fields, never free-text filters', () => {
    const scope = toolScope(ARGS)
    expect(scope).toEqual([
      { label: 'Window', value: '17:40–17:55 UTC' },
      { label: 'Identity', value: 'service' },
      { label: 'Service', value: 'inquest-demo' },
      { label: 'Severity', value: 'ERROR' },
    ])
    expect(JSON.stringify(scope)).not.toMatch(/hunter2|user\.email|a@b\.c/)
  })

  it('reads a truncated preview field by field', () => {
    const truncated =
      '{"time_range":{"start":"2026-10-21T17:40:00Z","end":"2026-10-21T17:55:00Z"},"service":"inquest-demo","group_by":"operation","where":[{"key":"http.ro…'
    expect(toolScope(truncated).map((s) => s.label)).toEqual(['Window', 'Service', 'Group by'])
    expect(toolScope(null)).toEqual([])
  })
})
