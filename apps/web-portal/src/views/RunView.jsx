import React, { useEffect, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Empty,
  Form,
  Input,
  List,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { subscribeRunStream } from '../sse.js'
import { platformApi } from '../api.js'

const STATUS_COLOR = {
  running: 'blue',
  completed: 'green',
  waiting_approval: 'orange',
  supplement_pending: 'purple',
  failed: 'red',
}

// Live run view: show historical platform runs, drill into persisted traces,
// and optionally subscribe to the SSE stream for a specific run id.
export default function RunView() {
  const [runIdInput, setRunIdInput] = useState('')
  const [activeRunId, setActiveRunId] = useState('')
  const [events, setEvents] = useState([])
  const [runs, setRuns] = useState([])
  const [runsLoading, setRunsLoading] = useState(false)
  const [trace, setTrace] = useState(null)
  const [traceLoadingRunId, setTraceLoadingRunId] = useState('')
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState(null)
  // R36-A: create-run form state.
  const [templates, setTemplates] = useState([])
  const [templatesLoading, setTemplatesLoading] = useState(false)
  const [creating, setCreating] = useState(false)
  const [createForm] = Form.useForm()
  const cleanupRef = useRef(null)

  const loadRuns = async () => {
    setRunsLoading(true)
    try {
      const data = await platformApi.listRuns({ page: 1, page_size: 20 })
      setRuns(data.items || [])
    } catch (e) {
      message.error(`加载运行记录失败: ${e.message}`)
    } finally {
      setRunsLoading(false)
    }
  }

  // R36-A: load the template list to populate the create-run Select.
  const loadTemplates = async () => {
    setTemplatesLoading(true)
    try {
      const data = await platformApi.listTemplates()
      setTemplates(data.items || [])
    } catch (e) {
      message.error(`加载模板列表失败: ${e.message}`)
    } finally {
      setTemplatesLoading(false)
    }
  }

  // R36-A: submit the create-run form. Maps the single text input to
  // {query: value} (the knowledge_qa contract) and auto-subscribes to
  // the new run's SSE stream so the timeline populates immediately.
  const onCreateRun = async (values) => {
    setCreating(true)
    try {
      const body = {
        template_id: values.template_id,
        requested_by: values.requested_by,
        input: { query: values.query || '' },
      }
      const run = await platformApi.createRun(body)
      message.success(`已创建运行 ${run.run_id}`)
      startStream(run.run_id)
      createForm.resetFields()
    } catch (e) {
      message.error(`创建运行失败: ${e.message}`)
    } finally {
      setCreating(false)
    }
  }

  const openTrace = async (runId) => {
    setTraceLoadingRunId(runId)
    try {
      const data = await platformApi.getRunTrace(runId)
      setTrace(data)
    } catch (e) {
      message.error(`加载运行详情失败: ${e.message}`)
    } finally {
      setTraceLoadingRunId('')
    }
  }

  const stopStream = () => {
    if (cleanupRef.current) {
      cleanupRef.current()
      cleanupRef.current = null
    }
    setConnected(false)
  }

  const startStream = (runId) => {
    stopStream()
    setEvents([])
    setError(null)
    setConnected(true)
    setActiveRunId(runId)
    cleanupRef.current = subscribeRunStream(
      runId,
      (ev) => {
        setEvents((prev) => [ev, ...prev])
      },
      (err) => {
        setConnected(false)
        setError(err?.message || 'stream error')
      },
    )
  }

  const onWatch = () => {
    const id = runIdInput.trim()
    if (!id) return
    startStream(id)
  }

  useEffect(() => {
    loadRuns()
    loadTemplates()
    return () => {
      if (cleanupRef.current) cleanupRef.current()
    }
  }, [])

  const columns = [
    { title: 'Run ID', dataIndex: 'run_id', key: 'run_id' },
    { title: '模板', dataIndex: 'template_id', key: 'template_id' },
    { title: 'Agent', dataIndex: 'agent_name', key: 'agent_name' },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      render: (s) => <Tag color={STATUS_COLOR[s] || 'default'}>{s}</Tag>,
    },
    {
      title: '审批',
      dataIndex: 'approval_status',
      key: 'approval_status',
      render: (s) => <Tag>{s}</Tag>,
    },
    { title: 'Trace ID', dataIndex: 'trace_id', key: 'trace_id', ellipsis: true },
    {
      title: '操作',
      key: 'action',
      render: (_, run) => (
        <Space>
          <Button
            size="small"
            onClick={() => openTrace(run.run_id)}
            loading={traceLoadingRunId === run.run_id}
          >
            查看详情
          </Button>
          <Button
            size="small"
            onClick={() => {
              setRunIdInput(run.run_id)
              startStream(run.run_id)
            }}
          >
            Watch
          </Button>
        </Space>
      ),
    },
  ]

  return (
    <div>
      <h2 className="page-title">运行实况 (Run Live)</h2>

      <Card
        title="最近运行记录"
        size="small"
        style={{ marginBottom: 16 }}
        extra={<Button size="small" onClick={loadRuns} loading={runsLoading}>刷新</Button>}
      >
        <Table
          rowKey="run_id"
          columns={columns}
          dataSource={runs}
          loading={runsLoading}
          pagination={{ pageSize: 8 }}
          size="small"
          locale={{ emptyText: '暂无运行记录' }}
        />
      </Card>

      <Card title="创建运行 (Create Run)" size="small" style={{ marginBottom: 16 }}>
        <Form
          form={createForm}
          layout="inline"
          onFinish={onCreateRun}
          initialValues={{ requested_by: 'web-portal' }}
        >
          <Form.Item
            name="template_id"
            label="模板"
            rules={[{ required: true, message: '请选择模板' }]}
          >
            <Select
              placeholder="选择模板"
              loading={templatesLoading}
              style={{ width: 200 }}
              options={templates.map((t) => ({
                value: t.template_id,
                label: t.template_id,
              }))}
            />
          </Form.Item>
          <Form.Item
            name="requested_by"
            label="请求人"
            rules={[{ required: true, message: '请输入请求人' }]}
          >
            <Input placeholder="requested_by" style={{ width: 160 }} />
          </Form.Item>
          <Form.Item
            name="query"
            label="问题"
            rules={[{ required: true, message: '请输入问题' }]}
          >
            <Input placeholder="输入问题 (query)" style={{ width: 280 }} />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" loading={creating}>
              创建运行
            </Button>
          </Form.Item>
        </Form>
      </Card>

      <Card size="small" style={{ marginBottom: 16 }}>
        <Space.Compact style={{ width: '100%' }}>
          <Input
            placeholder="输入运行 ID (run_id)"
            value={runIdInput}
            onChange={(e) => setRunIdInput(e.target.value)}
            onPressEnter={onWatch}
            allowClear
          />
          <Button type="primary" onClick={onWatch}>
            Watch
          </Button>
        </Space.Compact>
      </Card>

      {activeRunId && (
        <Space style={{ marginBottom: 12 }}>
          <Typography.Text strong>Run:</Typography.Text>
          <Typography.Text code>{activeRunId}</Typography.Text>
          <Tag color={connected ? 'green' : 'default'}>
            {connected ? '已连接 (connected)' : '已断开 (disconnected)'}
          </Tag>
        </Space>
      )}

      {error && (
        <Alert
          type="warning"
          message={error}
          style={{ marginBottom: 16 }}
          closable
          onClose={() => setError(null)}
        />
      )}

      <Card title="事件时间线 (Event Timeline)" size="small">
        {events.length === 0 ? (
          <Empty description="暂无事件 (waiting for events)" />
        ) : (
          <ul style={{ listStyle: 'none', padding: 0, margin: 0 }}>
            {events.map((ev, idx) => (
              <li
                key={`${ev.ts}-${idx}`}
                style={{
                  padding: '8px 0',
                  borderBottom: '1px solid #f0f0f0',
                }}
              >
                <Space direction="vertical" size={2} style={{ width: '100%' }}>
                  <Space>
                    <Tag color="blue">{ev.event_type}</Tag>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      {ev.ts}
                    </Typography.Text>
                  </Space>
                  <Typography.Text code style={{ fontSize: 12, wordBreak: 'break-all' }}>
                    {JSON.stringify(ev.payload ?? {})}
                  </Typography.Text>
                </Space>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Modal
        title={`运行详情 ${trace?.run?.run_id || ''}`}
        open={!!trace}
        onCancel={() => setTrace(null)}
        footer={null}
        width={920}
      >
        {trace && (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Descriptions column={2} size="small" bordered>
              <Descriptions.Item label="模板">{trace.run.template_id}</Descriptions.Item>
              <Descriptions.Item label="状态">
                <Tag color={STATUS_COLOR[trace.run.status] || 'default'}>{trace.run.status}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="请求人">{trace.run.requested_by}</Descriptions.Item>
              <Descriptions.Item label="审批">{trace.run.approval_status}</Descriptions.Item>
              <Descriptions.Item label="Trace ID" span={2}>
                {trace.run.trace_id}
              </Descriptions.Item>
            </Descriptions>
            <Card title="输入 / 输出" size="small">
              <pre style={{ margin: 0, maxHeight: 220, overflow: 'auto' }}>
                {JSON.stringify({ input: trace.run.input, output: trace.run.output }, null, 2)}
              </pre>
            </Card>
            <List
              header={<Typography.Text strong>节点轨迹</Typography.Text>}
              size="small"
              dataSource={trace.node_trace || []}
              renderItem={(node) => (
                <List.Item>
                  <List.Item.Meta
                    title={
                      <Space>
                        <Tag color={node.status === 'completed' ? 'green' : 'orange'}>
                          {node.status}
                        </Tag>
                        <Typography.Text>{node.node_name}</Typography.Text>
                      </Space>
                    }
                    description={node.detail}
                  />
                </List.Item>
              )}
              locale={{ emptyText: '暂无节点轨迹' }}
            />
            <List
              header={<Typography.Text strong>工具调用</Typography.Text>}
              size="small"
              dataSource={trace.tool_calls || []}
              renderItem={(tool) => (
                <List.Item>
                  <Space>
                    <Typography.Text>{tool.tool_name}</Typography.Text>
                    <Tag>{tool.risk_level}</Tag>
                    <Tag color={tool.status === 'success' ? 'green' : 'default'}>{tool.status}</Tag>
                  </Space>
                </List.Item>
              )}
              locale={{ emptyText: '暂无工具调用' }}
            />
          </Space>
        )}
      </Modal>
    </div>
  )
}
