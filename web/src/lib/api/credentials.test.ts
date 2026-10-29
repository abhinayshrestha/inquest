import { describe, expect, it } from 'vitest'

import {
  credentialStatus,
  nebiusCredentialsInput,
  signozCredentialsInput,
  VALIDATION_MESSAGES,
} from './credentials'

// Mirrors tests/test_environments_api.py: the frontend and backend contracts agree.
const NEBIUS = { api_key: 'x'.repeat(32), main_model: 'nvidia/main', local_model: 'nvidia/local' }

describe('credential contract', () => {
  it('accepts valid Nebius credentials and defaults the https base URL', () => {
    const parsed = nebiusCredentialsInput.parse(NEBIUS)
    expect(parsed.base_url).toBe('https://api.tokenfactory.nebius.com/v1/')
  })

  it.each([
    [{ base_url: 'http://api.tokenfactory.nebius.com/v1/' }, 'Must be an https:// URL'],
    [{ base_url: 'https://user:pass@api.example/v1/' }, 'Must not contain credentials'],
    [{ api_key: 'short' }, 'API keys are at least 20 characters'],
    [{ main_model: 'bad model id' }, 'Use the model id exactly as listed'],
  ])('rejects invalid Nebius input %j', (override, message) => {
    const result = nebiusCredentialsInput.safeParse({ ...NEBIUS, ...override })
    expect(result.success).toBe(false)
    expect(result.error?.issues.map((i) => i.message)).toContain(message)
  })

  it('rejects unknown fields and bad SigNoz URLs', () => {
    expect(nebiusCredentialsInput.safeParse({ ...NEBIUS, extra: 1 }).success).toBe(false)
    expect(
      signozCredentialsInput.safeParse({ base_url: 'ftp://signoz', api_key: 'x'.repeat(16) })
        .success,
    ).toBe(false)
    expect(
      signozCredentialsInput.safeParse({
        base_url: 'http://localhost:8080',
        api_key: 'x'.repeat(16),
      }).success,
    ).toBe(true)
  })

  it('describes stored credentials without any secret field', () => {
    const status = {
      kind: 'nebius',
      configured: true,
      state: 'configured',
      updated_at: null,
      updated_by: null,
      main_model: 'nvidia/main',
      local_model: 'nvidia/local',
      isolation: null,
    }
    expect(credentialStatus.parse(status)).toEqual(status)
    expect(credentialStatus.safeParse({ ...status, api_key: 'sk-x' }).success).toBe(false)
    expect(credentialStatus.safeParse({ ...status, base_url: 'https://x' }).success).toBe(false)
    expect(credentialStatus.safeParse({ ...status, state: 'expired' }).success).toBe(false)
  })

  it('explains every validation code the backend sends (src/credentials/validation.py)', () => {
    expect(Object.keys(VALIDATION_MESSAGES).sort()).toEqual([
      'auth_failed',
      'connection_not_isolated',
      'dedication_unverified',
      'destination_not_allowed',
      'model_not_found',
      'provider_error',
      'scope_not_enforced',
      'tenant_keys_missing',
      'unreachable',
    ])
  })
})
