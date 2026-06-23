import { expect, test } from '@playwright/test'

const internalToken = process.env.INTERNAL_TOKEN || 'change-me'
const ragQuestion = '某 5G 小区出现 RRC 建立失败率升高，应该先查什么？'

let rcaRun

test.describe.configure({ mode: 'serial' })

test.beforeAll(async ({ request }) => {
  const upload = await request.post('/api/knowledge/api/v1/documents', {
    headers: { 'X-Internal-Token': internalToken },
    multipart: {
      title: `web-e2e-rag-${Date.now()}`,
      mime_type: 'text/markdown',
      file: {
        name: 'project-1-rag-knowledge-base-design-spec.md',
        mimeType: 'text/markdown',
        buffer: await import('node:fs/promises').then((fs) =>
          fs.readFile('../../Docs/project-1-rag-knowledge-base-design-spec.md'),
        ),
      },
    },
  })
  expect(upload.ok()).toBeTruthy()
  const doc = await upload.json()

  const publish = await request.post(
    `/api/knowledge/api/v1/documents/${doc.doc_id}/publish`,
    { headers: { 'X-Internal-Token': internalToken } },
  )
  expect(publish.ok()).toBeTruthy()

  const sample = await import('node:fs/promises').then(async (fs) => {
    const raw = await fs.readFile('../../tests/rca-replay/sample_cases.jsonl', 'utf8')
    return JSON.parse(raw.split(/\r?\n/)[0])
  })
  const run = await request.post('/api/rca/api/v1/rca/runs', {
    headers: { 'X-Internal-Token': internalToken },
    data: {
      alarms: sample.alarms,
      require_human_review: true,
    },
  })
  expect(run.ok()).toBeTruthy()
  rcaRun = await run.json()
})

test('平台总览 renders with operations widgets', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByText('平台总览')).toBeVisible()
  await expect(page.getByText('工具调用成功率')).toBeVisible()
  await expect(page.getByText('Prometheus 指标')).toBeVisible()
})

test('知识库 query returns answer and 引用证据', async ({ page }) => {
  await page.goto('/')
  await page.getByText('知识库', { exact: true }).click()
  await expect(page.getByText('文档列表')).toBeVisible()
  await page.getByPlaceholder('输入运维问题').fill(ragQuestion)
  await page.getByRole('button', { name: '检索问答' }).click()
  await expect(page.getByText('问答结果')).toBeVisible()
  await expect(page.getByText('引用证据')).toBeVisible()
})

test('RCA 诊断 lists a run and opens 查看报告', async ({ page }) => {
  await page.goto('/')
  await page.getByText('RCA 诊断', { exact: true }).click()
  await page.getByRole('button', { name: '刷 新' }).click()
  const runRow = page.locator('tr', { hasText: rcaRun.run_id })
  await expect(runRow).toBeVisible()
  await expect(runRow.getByText('waiting_review')).toBeVisible()
  await runRow.getByText('查看报告', { exact: true }).click()
  await expect(page.getByText(`RCA 报告 ${rcaRun.report_id}`)).toBeVisible()
  await expect(page.getByText('Top-N 根因候选')).toBeVisible()
})
