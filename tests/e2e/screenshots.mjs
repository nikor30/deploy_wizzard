// Capture README screenshots against the running app (:8061) + mock stack (:9100).
// Usage:  node tests/e2e/screenshots.mjs  (servers must already be up)
import { chromium } from '@playwright/test'
import { mkdirSync } from 'node:fs'

const APP = 'http://127.0.0.1:8061'
const MOCK = 'http://127.0.0.1:9100'
const OUT = 'docs/screenshots'

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch({
  executablePath: process.env.PW_CHROMIUM_PATH || undefined,
})
const page = await browser.newPage({
  viewport: { width: 1360, height: 850 },
  deviceScaleFactor: 2,
})
const api = page.request

async function shot(name) {
  await page.waitForTimeout(300)
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false })
  console.log('captured', name)
}

// --- seed: mocks reset, app configured, secrets + dayn mappings stored ------
await api.post(`${MOCK}/__mock__/reset`, { data: { devices: 3 } })
await api.put(`${APP}/api/settings/credentials`, {
  data: {
    catalyst: { base_url: `${MOCK}/ccc`, username: 'admin', secret: 'ccc-password-77' },
    netbox: { base_url: `${MOCK}/netbox`, secret: 'netbox-token-4711' },
    webhook: { base_url: `${MOCK}/ise/hook`, secret: 'ise-hmac-secret', enabled: true },
  },
})
await api.put(`${APP}/api/settings/secrets/radius_key`, { data: { secret: 'radius-key-123' } })
const jobs = await (await api.get(`${APP}/api/wizard/jobs`)).json()
for (const j of jobs) await api.delete(`${APP}/api/wizard/jobs/${j.id}`)

// --- settings: credentials ---------------------------------------------------
await page.goto(`${APP}/settings/credentials`)
await page.waitForSelector('text=Catalyst Center')
await shot('settings-credentials')

// --- settings: site mapping with a live suggestion ---------------------------
await api.put(`${APP}/api/mappings/sites`, { data: { mappings: [] } })
await page.goto(`${APP}/settings/mapping`)
await page.waitForSelector('text=FFM-DC1')
await page.getByRole('button', { name: 'Suggest mappings' }).click()
await page.waitForSelector('text=suggested')
await shot('settings-mapping')
await page.getByRole('button', { name: 'Save mappings' }).click()
await page.waitForSelector('text=Mappings saved.')

// --- settings: day-n variables (secrets + suggestions) -----------------------
await api.put(`${APP}/api/settings/dayn`, { data: { mappings: [] } })
await page.goto(`${APP}/settings/dayn`)
await page.waitForSelector('text=Template secrets')
await page.getByRole('combobox').selectOption({ label: 'Baseline / DayN Baseline' })
await page.getByRole('button', { name: 'Suggest mappings' }).click()
await page.waitForSelector('text=suggested ·')
await shot('settings-dayn')
await page.getByRole('button', { name: 'Save mappings' }).click()

// --- wizard: select devices --------------------------------------------------
await page.goto(APP)
await page.getByRole('button', { name: 'Start new onboarding job' }).click()
await page.waitForSelector('text=SN000001')
await page.getByLabel('Select SN000001').check()
await page.getByLabel('Select SN000002').check()
await shot('wizard-select')

// --- wizard: match view ------------------------------------------------------
await page.getByRole('button', { name: /Continue with 2/ }).click()
await page.waitForSelector('text=same serial number')
await page.waitForSelector('text=Continue to Day-0 claim (2 device(s))')
await page
  .getByRole('region', { name: 'Match SN000001' })
  .getByLabel(/Mgmt VLAN/i)
  .selectOption('110')
await page
  .getByRole('region', { name: 'Match SN000002' })
  .getByLabel(/Mgmt VLAN/i)
  .selectOption('110')
await shot('wizard-match')

// --- wizard: day-0 claim with live progress ----------------------------------
await page.getByRole('button', { name: 'Continue to Day-0 claim (2 device(s))' }).click()
await page.getByLabel(/Onboarding template/i).selectOption({ label: 'PnP / Day0 Onboarding' })
await page.getByRole('button', { name: 'Start Day-0 claim (2 device(s))' }).click()
await page.waitForSelector('text=Day-0 finished: 2 succeeded, 0 failed', { timeout: 60000 })
await shot('wizard-day0')

// --- wizard: day-n with resolved + manual + secret variables -----------------
await page.getByRole('button', { name: 'Continue to Day-N (2 device(s))' }).click()
await page.getByRole('combobox').selectOption({ label: 'Baseline / DayN Baseline' })
await page.getByRole('button', { name: 'Resolve variables' }).click()
await page.waitForSelector('text=FFM DC1 / Rack 4')
for (const input of await page.getByLabel(/CONTACT \(manual\)/i).all()) {
  await input.fill('noc@example.com')
}
await shot('wizard-dayn')

// --- wizard: finalize summary ------------------------------------------------
await page.getByRole('button', { name: 'Deploy Day-N (2 device(s))' }).click()
await page.waitForSelector('text=active in NetBox', { timeout: 60000 })
await shot('wizard-summary')

// --- stats + logs ------------------------------------------------------------
await page.goto(`${APP}/stats`)
await page.waitForTimeout(1200)
await shot('stats')
await page.goto(`${APP}/logs`)
await page.waitForTimeout(1200)
await shot('logs')

await browser.close()
console.log('done')
