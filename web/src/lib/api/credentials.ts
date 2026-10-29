import { z } from '@/lib/zod'

import { request } from './client'

// Per-environment credential contract (D026, D034). Mirrors src/schemas/credentials.py and
// the routes in src/api/routes/credentials.py. Writes are owner-only on the server; the
// page only offers them to the owner. Secrets travel in the request body of one PUT and
// are never returned: reads give CredentialStatus only. Nothing here is cached or stored.

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

// dedicated: the organization's own SigNoz. provider_scoped: a shared SigNoz Cloud
// workspace whose key is restricted to ingestion keys an operator assigned (D037).
export const telemetryIsolation = z.enum(['dedicated', 'provider_scoped'])

export const signozCredentialsInput = z.strictObject({
  base_url: url(false),
  api_key: z.string().min(8, 'API keys are at least 8 characters').max(512),
  isolation: telemetryIsolation.default('dedicated'),
})

// The only thing the backend will ever return about a stored credential.
export const credentialStatus = z.strictObject({
  kind: z.enum(['nebius', 'signoz']),
  configured: z.boolean(),
  // unavailable: stored, but the server cannot decrypt it; treated like not configured.
  state: z.enum(['not_configured', 'configured', 'unavailable']),
  updated_at: z.iso.datetime({ offset: true }).nullable(),
  updated_by: z.string().nullable(),
  // Nebius only: model ids are configuration, not secrets.
  main_model: z.string().nullable(),
  local_model: z.string().nullable(),
  // SigNoz only.
  isolation: telemetryIsolation.nullable(),
})

export type NebiusCredentialsInput = z.infer<typeof nebiusCredentialsInput>
export type SignozCredentialsInput = z.infer<typeof signozCredentialsInput>
export type CredentialStatus = z.infer<typeof credentialStatus>

export type CredentialKind = CredentialStatus['kind']
export type TelemetryIsolation = z.infer<typeof telemetryIsolation>

export const ISOLATION_LABEL: Record<TelemetryIsolation, string> = {
  dedicated: 'Dedicated SigNoz of this organization',
  provider_scoped: 'Shared workspace, restricted to this organization’s ingestion keys',
}

export const KIND_LABEL: Record<CredentialKind, string> = { nebius: 'Nebius', signoz: 'SigNoz' }

// Public default endpoint (not a stored value): the backend uses the same default.
export const NEBIUS_DEFAULT_URL = 'https://api.tokenfactory.nebius.com/v1/'

const enc = encodeURIComponent
const base = (environmentId: string) => `/environments/${enc(environmentId)}/credentials`

export const getCredentials = (environmentId: string, { signal }: { signal?: AbortSignal } = {}) =>
  request(base(environmentId), z.array(credentialStatus), { signal })

export const putNebiusCredentials = (environmentId: string, input: NebiusCredentialsInput) =>
  request(`${base(environmentId)}/nebius`, credentialStatus, { method: 'PUT', json: input })

export const putSignozCredentials = (environmentId: string, input: SignozCredentialsInput) =>
  request(`${base(environmentId)}/signoz`, credentialStatus, { method: 'PUT', json: input })

export const deleteCredential = (environmentId: string, kind: CredentialKind) =>
  request(`${base(environmentId)}/${kind}`, z.null(), { method: 'DELETE' })

// Validation refusals (src/credentials/validation.py codes), in the page's own words. The
// provider's response is never shown: the server does not even return it.
export const VALIDATION_MESSAGES: Record<string, string> = {
  destination_not_allowed:
    "This server's destination policy does not allow that endpoint. Nebius must be an allowlisted https origin. SigNoz must be a public https endpoint, unless an operator allowlisted the exact origin: local, private and plain-http endpoints work only that way.",
  auth_failed: 'The provider rejected this API key. Nothing was saved.',
  model_not_found:
    'This key cannot use one of these model ids. Check them against your Token Factory model list. Nothing was saved.',
  unreachable:
    'The provider could not be reached or was unavailable. Nothing was saved; try again later.',
  provider_error: 'The provider answered unexpectedly. Nothing was saved.',
  connection_not_isolated:
    'This SigNoz cannot be accepted as dedicated to this organization. Use your own SigNoz instance or workspace, or a provider-scoped workspace the operator set up for you. Nothing was saved.',
  dedication_unverified:
    'An operator has not verified this SigNoz as dedicated to this organization, which this server requires. Ask the operator to verify it, or use a provider-scoped workspace. Nothing was saved.',
  tenant_keys_missing:
    'No ingestion keys of this workspace are assigned to this organization yet. Ask the operator to assign them, then try again. Nothing was saved.',
  scope_not_enforced:
    'This key can read telemetry beyond your organization’s ingestion keys. Use the API key of a service account whose only role is scoped to your keys. Nothing was saved.',
}
