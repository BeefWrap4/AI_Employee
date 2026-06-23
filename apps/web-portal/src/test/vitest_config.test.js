import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

// Guards against R36-D regression: vitest must NOT load the Playwright
// e2e spec files, which use test.describe.configure() — a Playwright-only
// API that crashes vitest (1 failed suite even though all unit tests pass).
describe('vitest config excludes the e2e directory', () => {
  const configPath = resolve(process.cwd(), 'vitest.config.js')
  const configSrc = readFileSync(configPath, 'utf8')

  it('declares a test.exclude array', () => {
    expect(configSrc).toMatch(/test\s*:\s*\{[\s\S]*?exclude\s*:/)
  })

  it('excludes the e2e glob so Playwright specs are not collected', () => {
    expect(configSrc).toMatch(/\*\*\/e2e\/\*\*/)
  })
})
