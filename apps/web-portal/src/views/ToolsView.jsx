import React, { useEffect, useState } from 'react'
import {
  Button,
  Card,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { toolsApi } from '../api.js'

const RISK_COLOR = {
  read_only: 'green',
  approval_required: 'orange',
  high_risk: 'red',
}

// Tool registry view: list registered tools (MCP tools/list shape) and
// invoke read-only tools with arbitrary JSON arguments.
export default function ToolsView() {
  const [tools, setTools] = useState([])
  const [invokeTarget, setInvokeTarget] = useState(null)
  const [argsText, setArgsText] = useState('{}')
  const [invokeResult, setInvokeResult] = useState(null)
  const [loading, setLoading] = useState(false)

  const loadTools = async () => {
    try {
      const data = await toolsApi.list()
      setTools(data.tools || [])
    } catch (e) {
      message.error(`加载工具失败: ${e.message}`)
    }
  }

  useEffect(() => {
    loadTools()
  }, [])

  const doInvoke = async () => {
    let args
    try {
      args = JSON.parse(argsText)
    } catch (e) {
      message.error('参数不是合法 JSON')
      return
    }
    setLoading(true)
    try {
      const result = await toolsApi.invoke(invokeTarget.name, args)
      setInvokeResult(result)
    } catch (e) {
      message.error(`调用失败: ${e.message}`)
    } finally {
      setLoading(false)
    }
  }

  const columns = [
    { title: '工具名', dataIndex: 'name', key: 'name' },
    { title: '描述', dataIndex: 'description', key: 'description', ellipsis: true },
    {
      title: '风险等级',
      key: 'risk_level',
      render: (_, t) => (
        <Tag color={RISK_COLOR[t.metadata?.risk_level] || 'default'}>
          {t.metadata?.risk_level || 'unknown'}
        </Tag>
      ),
    },
    { title: '服务', dataIndex: ['metadata', 'service_name'], key: 'service_name' },
    {
      title: '操作',
      key: 'action',
      render: (_, t) => (
        <Button
          size="small"
          disabled={t.metadata?.risk_level !== 'read_only'}
          onClick={() => {
            setInvokeTarget(t)
            setArgsText('{}')
            setInvokeResult(null)
          }}
        >
          调用
        </Button>
      ),
    },
  ]

  return (
    <div>
      <h2 className="page-title">工具注册</h2>
      <Card size="small">
        <Table
          rowKey="name"
          columns={columns}
          dataSource={tools}
          pagination={{ pageSize: 10 }}
          size="small"
        />
      </Card>

      <Modal
        title={`调用工具: ${invokeTarget?.name || ''}`}
        open={!!invokeTarget}
        onCancel={() => setInvokeTarget(null)}
        footer={[
          <Button key="cancel" onClick={() => setInvokeTarget(null)}>
            关闭
          </Button>,
          <Button key="invoke" type="primary" loading={loading} onClick={doInvoke}>
            执行
          </Button>,
        ]}
      >
        <Typography.Paragraph type="secondary">
          {invokeTarget?.description}
        </Typography.Paragraph>
        <Form layout="vertical">
          <Form.Item label="参数 (JSON)">
            <Input.TextArea
              rows={4}
              value={argsText}
              onChange={(e) => setArgsText(e.target.value)}
            />
          </Form.Item>
        </Form>
        {invokeResult && (
          <Card title="返回结果" size="small">
            <pre style={{ maxHeight: 240, overflow: 'auto', margin: 0 }}>
              {JSON.stringify(invokeResult, null, 2)}
            </pre>
          </Card>
        )}
      </Modal>
    </div>
  )
}
