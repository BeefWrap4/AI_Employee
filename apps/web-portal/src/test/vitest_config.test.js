import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

// Guards against R36-D regression: vitest must NOT load the Playwright
// e2e spec files, which use test.describe.configure() — a Playwright-only
// API that crashes vitest (1 failed suite even though all unit tests pass).
// Also guards the production-bundle code-splitting: vite.config.js must
// declare a manualChunks function so react/antd/echarts vendor libs land in
// separate chunks and the main bundle stays under the 500 kB warning limit.
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

describe('vite config code-splits the production bundle', () => {
  const configPath = resolve(process.cwd(), 'vite.config.js')
  const configSrc = readFileSync(configPath, 'utf8')

  it('declares build.rollupOptions.output.manualChunks', () => {
    expect(configSrc).toMatch(/manualChunks/)
  })

  it('splits react into its own vendor chunk', () => {
    expect(configSrc).toMatch(/react-vendor/)
  })

  it('splits antd into its own vendor chunk', () => {
    expect(configSrc).toMatch(/antd-vendor/)
  })

  it('splits echarts into its own vendor chunk', () => {
    expect(configSrc).toMatch(/echarts-vendor/)
  })
})
