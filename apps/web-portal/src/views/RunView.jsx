import React, { useEffect, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Empty,
  Input,
  List,
  Modal,
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
