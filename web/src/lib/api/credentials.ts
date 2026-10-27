import { z } from 'zod'

// Planned per-environment credential contract (D026). Mirrors src/schemas/credentials.py.
// No endpoint accepts credentials yet: without authentication anyone could set or replace
// them, and there is no encrypted storage (D027). The forms that use these schemas render
// disabled; nothing here is sent, stored in the browser or kept after the page closes.

const MODEL_ID = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$/

function url(httpsOnly: boolean) {
  return z
    .string()
    .trim()
    .max(500)
    .refine((v) => (httpsOnly ? /^https:\/\//.test(v) : /^https?:\/\//.test(v)), {
      message: httpsOnly ? 'Must be an https:// URL' : 'Must be an http(s):// URL',
    })
    .refine((v) => !/\s/.test(v), { message: 'Must not contain spaces' })
    .refine((v) => !(v.split('//', 2)[1] ?? '').split('/', 1)[0]!.includes('@'), {
      message: 'Must not contain credentials',
    })
}

export const nebiusCredentialsInput = z.strictObject({
  api_key: z.string().min(20, 'API keys are at least 20 characters').max(512),
  base_url: url(true).default('https://api.tokenfactory.nebius.com/v1/'),
  main_model: z.string().regex(MODEL_ID, 'Use the model id exactly as listed'),
  local_model: z.string().regex(MODEL_ID, 'Use the model id exactly as listed'),
})

export const signozCredentialsInput = z.strictObject({
  base_url: url(false),
  api_key: z.string().min(8, 'API keys are at least 8 characters').max(512),
})

// The only thing the backend will ever return about a stored credential.
export const credentialStatus = z.strictObject({
  kind: z.enum(['nebius', 'signoz']),
  configured: z.boolean(),
  updated_at: z.iso.datetime({ offset: true }).nullable(),
  updated_by: z.string().nullable(),
})

export type NebiusCredentialsInput = z.infer<typeof nebiusCredentialsInput>
export type SignozCredentialsInput = z.infer<typeof signozCredentialsInput>
export type CredentialStatus = z.infer<typeof credentialStatus>
