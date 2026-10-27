import { describe, expect, it } from 'vitest'

import { credentialStatus, nebiusCredentialsInput, signozCredentialsInput } from './credentials'

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
    const status = { kind: 'nebius', configured: true, updated_at: null, updated_by: null }
    expect(credentialStatus.parse(status)).toEqual(status)
    expect(credentialStatus.safeParse({ ...status, api_key: 'sk-x' }).success).toBe(false)
  })
})
