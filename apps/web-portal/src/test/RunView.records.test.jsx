import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import RunView from '../views/RunView.jsx'

function response(body) {
  return Promise.resolve({
    ok: true,
    status: 200,
    text: () => Promise.resolve(JSON.stringify(body)),
  })
}

describe('RunView run records', () => {
  afterEach(() => vi.restoreAllMocks())

  it('lists recent agent runs and opens a trace detail panel', async () => {
    global.fetch = vi.fn().mockImplementation((url) => {
      const s = String(url)
      if (s.endsWith('/api/platform/api/v1/agent-runs?page=1&page_size=20')) {
        return response({
          items: [
            {
              run_id: 'agent_run_demo_001',
              template_id: 'knowledge_qa',
              agent_name: '知识问答 Agent',
              status: 'completed',
              trace_id: 'trace-demo-001',
              requested_by: 'demo',
              approval_status: 'not_required',
            },
          ],
          total: 1,
          page: 1,
          page_size: 20,
        })
      }
      if (s.endsWith('/api/platform/api/v1/agent-runs/agent_run_demo_001/trace')) {
        return response({
          run: {
            run_id: 'agent_run_demo_001',
            template_id: 'knowledge_qa',
            agent_name: '知识问答 Agent',
            status: 'completed',
            trace_id: 'trace-demo-001',
            requested_by: 'demo',
            input: { question: 'RRC 建立失败率升高怎么排查？' },
            output: { answer: '先核查告警、KPI 和变更窗口。' },
            node_trace: [],
            tool_calls: [],
            approval_status: 'not_required',
          },
          template: { template_id: 'knowledge_qa', tool_names: [] },
          node_trace: [
            { node_name: 'Plan', status: 'completed', detail: '生成排查计划' },
            { node_name: 'Answer', status: 'completed', detail: '生成引用回答' },
          ],
          tool_calls: [{ tool_name: 'knowledge.search', risk_level: 'read_only', status: 'success' }],
          approval_tasks: [],
          registered_tools: [],
        })
      }
      return response({})
    })

    render(<RunView />)

    expect(await screen.findByText('最近运行记录')).toBeInTheDocument()
    expect(await screen.findByText('agent_run_demo_001')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '查看详情' }))

    expect(await screen.findByText('运行详情 agent_run_demo_001')).toBeInTheDocument()
    expect(screen.getByText('Plan')).toBeInTheDocument()
    expect(screen.getByText('knowledge.search')).toBeInTheDocument()
    expect(screen.getByText(/RRC 建立失败率升高/)).toBeInTheDocument()
  })
})
