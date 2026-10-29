import { z } from '@/lib/zod'

import { request } from './client'

// Environment ownership transfers (Phase 7C-3, D035). Mirrors src/schemas/transfers.py and
// src/api/routes/transfers.py. The owner proposes, the recipient accepts or declines, the
// proposer cancels; the server checks every step. Ids and states only.

const id = z.uuid()
const timestamp = z.iso.datetime({ offset: true })

export const transfer = z.object({
  id,
  environment_id: id,
  environment_name: z.string(),
  organization_id: id,
  from_user_id: id,
  to_user_id: id,
  status: z.enum(['pending', 'accepted', 'declined', 'cancelled']),
  direction: z.enum(['incoming', 'outgoing']),
  created_at: timestamp,
  resolved_at: timestamp.nullable(),
})

export type Transfer = z.infer<typeof transfer>

const enc = encodeURIComponent

export const TRANSFERS_KEY = ['transfers'] as const

export const listTransfers = ({ signal }: { signal?: AbortSignal } = {}) =>
  request('/transfers', z.array(transfer), { signal })

export const proposeTransfer = (environmentId: string, toUserId: string) =>
  request(`/environments/${enc(environmentId)}/transfers`, transfer, {
    method: 'POST',
    json: { to_user_id: toUserId },
  })

export const resolveTransfer = (transferId: string, action: 'accept' | 'decline' | 'cancel') =>
  request(`/transfers/${enc(transferId)}/${action}`, transfer, { method: 'POST' })
