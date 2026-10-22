// Shaped like real GET /investigations/{id} responses (src/schemas/investigation.py),
// including Pydantic's timestamp formats.
export const INVESTIGATION_ID = '89efc27e-1111-4c4c-8d8d-0123456789ab'
const TURN_ID = '1b2c3d4e-2222-4c4c-8d8d-0123456789ab'
const ENV = {
  id: '0f0e0d0c-3333-4c4c-8d8d-0123456789ab',
  name: 'ubuntu-vm',
  type: 'local',
  status: 'active',
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
  assignments: [
    {
      id: '9a8b7c6d-4444-4c4c-8d8d-0123456789ab',
      turn_id: TURN_ID,
      round: 1,
      objective: 'Check latency and errors',
      status: 'completed',
      started_at: '2026-10-21T17:30:02Z',
      completed_at: '2026-10-21T17:31:00Z',
      created_at: '2026-10-21T17:30:00Z',
      updated_at: '2026-10-21T17:31:00Z',
      environment: ENV,
    },
  ],
  turns: [
    {
      id: TURN_ID,
      seq: 1,
      kind: 'initial',
      question: 'Why is my API slow?',
      status: 'completed',
      decision: 'investigated',
      answer_json: { answer_md: 'GET /slow p95 ≈ 1.5 s [ev_1f5967bf]', refs: ['ev_1f5967bf'] },
      error_code: null,
      started_at: '2026-10-21T17:30:01Z',
      completed_at: '2026-10-21T17:31:34Z',
      created_at: '2026-10-21T17:30:00Z',
    },
  ],
  reports: [
    {
      id: '5e6f7a8b-5555-4c4c-8d8d-0123456789ab',
      turn_id: TURN_ID,
      version: 1,
      created_at: '2026-10-21T17:31:34Z',
    },
  ],
}
