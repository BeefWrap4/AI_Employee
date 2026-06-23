import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.js'],
    css: false,
    // The Playwright e2e specs under e2e/** use test.describe.configure(),
    // a Playwright-only API that crashes vitest collection. Playwright has its
    // own config (playwright.config.js, testDir: './e2e') so `npx playwright
    // test` still picks them up — vitest must exclude the directory.
    exclude: ['**/e2e/**', '**/node_modules/**', '**/dist/**'],
  },
})
