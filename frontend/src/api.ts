export interface ServiceSettings {
  base_url: string | null
  username: string | null
  secret_masked: string | null
  tls_verify: boolean
  enabled: boolean
  configured: boolean
}

export interface Credentials {
  catalyst: ServiceSettings
  netbox: ServiceSettings
  webhook: ServiceSettings
}

export interface ServiceSettingsInput {
  base_url: string | null
  username?: string | null
  secret?: string | null
  tls_verify?: boolean
  enabled?: boolean
}

export interface TestResult {
  ok: boolean
  detail: string
}

async function check(res: Response): Promise<Response> {
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const body = (await res.json()) as { detail?: string }
      if (body.detail) detail = body.detail
    } catch {
      /* keep the status text */
    }
    throw new Error(detail)
  }
  return res
}

export async function getCredentials(): Promise<Credentials> {
  const res = await check(await fetch('/api/settings/credentials'))
  return res.json() as Promise<Credentials>
}

export async function putCredentials(
  payload: Partial<Record<'catalyst' | 'netbox' | 'webhook', ServiceSettingsInput>>,
): Promise<Credentials> {
  const res = await check(
    await fetch('/api/settings/credentials', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  )
  return res.json() as Promise<Credentials>
}

export async function testConnection(
  service: 'catalyst' | 'netbox',
  payload: ServiceSettingsInput,
): Promise<TestResult> {
  const res = await check(
    await fetch(`/api/settings/credentials/${service}/test`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  )
  return res.json() as Promise<TestResult>
}
