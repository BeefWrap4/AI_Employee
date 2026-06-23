import React, { useEffect, useRef, useState } from 'react'
import { Alert, Button, Card, Input, Space, Tag, Typography, Empty } from 'antd'
import { subscribeRunStream } from '../sse.js'

// Live run view: enter a run id, click "Watch", and stream the run's
// SSE event timeline (R33-D backend endpoint
// /api/v1/agent-runs/{run_id}/stream on agent-platform-api:8030).
//
// Events accumulate in state and render newest-first.  The EventSource
// is closed on unmount or when a new run is watched.
export default function RunView() {
  const [runIdInput, setRunIdInput] = useState('')
  const [activeRunId, setActiveRunId] = useState('')
  const [events, setEvents] = useState([])
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState(null)
  const cleanupRef = useRef(null)

  const stopStream = () => {
    if (cleanupRef.current) {
      cleanupRef.current()
      cleanupRef.current = null
    }
    setConnected(false)
  }

  const startStream = (runId) => {
    // Tear down any previous subscription before opening a new one.
    stopStream()
    setEvents([])
    setError(null)
    setConnected(true)
    setActiveRunId(runId)
    cleanupRef.current = subscribeRunStream(
      runId,
      (ev) => {
        // Prepend so the newest event renders first.
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

  // Close the stream when the component unmounts.
  useEffect(() => {
    return () => {
      if (cleanupRef.current) cleanupRef.current()
    }
  }, [])

  return (
    <div>
      <h2 className="page-title">运行实况 (Run Live)</h2>
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
    </div>
  )
}
