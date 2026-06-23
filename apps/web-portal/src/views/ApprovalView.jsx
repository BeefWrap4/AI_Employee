import React, { useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  message,
} from 'antd'
import { approvalApi } from '../api.js'

const STATUS_COLOR = {
  pending: 'orange',
  approved: 'green',
  rejected: 'red',
  transferred: 'blue',
  escalated: 'purple',
  supplement_pending: 'cyan',
  expired: 'default',
}

const RISK_COLOR = {
  low: 'green',
  medium: 'orange',
  high_risk: 'red',
  high: 'red',
}

// Approval task management view. Lists platform approval tasks with
// status/risk columns and per-row actions: decide (approve/reject),
// transfer, escalate. Each action opens a Modal collecting the fields
// the backend endpoint expects, then calls the matching approvalApi
// method and refreshes the list.
export default function ApprovalView() {
  const [tasks, setTasks] = useState([])
  const [loading, setLoading] = useState(false)
  const [statusFilter, setStatusFilter] = useState('pending')
  const [error, setError] = useState(null)
  // action = { kind: 'decide'|'transfer'|'escalate', task }
  const [action, setAction] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const [form] = Form.useForm()

  const loadTasks = async () => {
    setLoading(true)
    setError(null)
    try {
      const query = { page: 1, page_size: 50 }
      if (statusFilter) query.status = statusFilter
      const data = await approvalApi.listTasks(query)
      setTasks(data.items || [])
    } catch (e) {
      setError(e.message)
      message.error(`加载审批任务失败: ${e.message}`)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadTasks()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter])

  const openAction = (kind, task) => {
    setAction({ kind, task })
    form.resetFields()
  }

  const closeAction = () => {
    setAction(null)
    form.resetFields()
  }

  const submitAction = async () => {
    let values
    try {
      values = await form.validateFields()
    } catch {
      return
    }
    const { task, kind } = action
    setSubmitting(true)
    try {
      if (kind === 'decide') {
        await approvalApi.decide(task.task_id, {
          decision: values.decision,
          decided_by: values.decided_by,
          comment: values.comment,
        })
      } else if (kind === 'transfer') {
        await approvalApi.transfer(task.task_id, {
          new_approver: values.new_approver,
          reason: values.reason,
        })
      } else if (kind === 'escalate') {
        await approvalApi.escalate(task.task_id, {
          escalated_to: values.escalated_to,
          reason: values.reason,
        })
      }
      message.success('操作成功')
      closeAction()
      await loadTasks()
    } catch (e) {
      message.error(`操作失败: ${e.message}`)
    } finally {
      setSubmitting(false)
    }
  }

  const columns = [
    { title: '任务 ID', dataIndex: 'task_id', key: 'task_id' },
    { title: 'Run ID', dataIndex: 'run_id', key: 'run_id' },
    { title: '模板', dataIndex: 'template_id', key: 'template_id' },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      render: (s) => <Tag color={STATUS_COLOR[s] || 'default'}>{s}</Tag>,
    },
    {
      title: '风险等级',
      dataIndex: 'risk_level',
      key: 'risk_level',
      render: (r) => <Tag color={RISK_COLOR[r] || 'default'}>{r || '-'}</Tag>,
    },
    {
      title: '原因',
      dataIndex: 'reason',
      key: 'reason',
      ellipsis: true,
    },
    { title: '创建时间', dataIndex: 'created_at', key: 'created_at', ellipsis: true },
    {
      title: '操作',
      key: 'action',
      render: (_, task) => (
        <Space size="small" wrap>
          <Button size="small" type="primary" onClick={() => openAction('decide', task)}>
            通过
          </Button>
          <Button
            size="small"
            danger
            onClick={() => openAction('decide', { ...task, _reject: true })}
          >
            拒绝
          </Button>
          <Button size="small" onClick={() => openAction('transfer', task)}>
            转交
          </Button>
          <Button size="small" onClick={() => openAction('escalate', task)}>
            升级
          </Button>
        </Space>
      ),
    },
  ]

  const modalTitle =
    action?.kind === 'decide'
      ? action.task?._reject
        ? `拒绝审批任务 ${action.task?.task_id}`
        : `通过审批任务 ${action.task?.task_id}`
      : action?.kind === 'transfer'
        ? `转交审批任务 ${action.task?.task_id}`
        : action?.kind === 'escalate'
          ? `升级审批任务 ${action.task?.task_id}`
          : ''

  return (
    <div>
      <h2 className="page-title">审批管理</h2>

      <Card
        size="small"
        title="审批任务列表"
        extra={
          <Space>
            <Select
              value={statusFilter}
              onChange={setStatusFilter}
              style={{ width: 160 }}
              options={[
                { value: 'pending', label: '待审批 (pending)' },
                { value: 'approved', label: '已通过 (approved)' },
                { value: 'rejected', label: '已拒绝 (rejected)' },
                { value: '', label: '全部 (all)' },
              ]}
            />
            <Button size="small" onClick={loadTasks} loading={loading}>
              刷新
            </Button>
          </Space>
        }
      >
        {error && (
          <Alert type="warning" message={error} style={{ marginBottom: 12 }} closable />
        )}
        <Table
          rowKey="task_id"
          columns={columns}
          dataSource={tasks}
          loading={loading}
          pagination={{ pageSize: 10 }}
          size="small"
          locale={{ emptyText: '暂无审批任务' }}
        />
      </Card>

      <Modal
        title={modalTitle}
        open={!!action}
        onCancel={closeAction}
        footer={[
          <Button key="cancel" onClick={closeAction}>
            取消
          </Button>,
          <Button key="submit" type="primary" loading={submitting} onClick={submitAction}>
            提交
          </Button>,
        ]}
      >
        <Form form={form} layout="vertical" initialValues={{ decision: 'approved' }}>
          {action?.kind === 'decide' && (
            <>
              <Form.Item name="decision" label="决策" rules={[{ required: true }]}>
                <Select
                  options={[
                    { value: 'approved', label: '通过 (approved)' },
                    { value: 'rejected', label: '拒绝 (rejected)' },
                  ]}
                  onChange={(v) =>
                    form.setFieldValue('decision', v)
                  }
                />
              </Form.Item>
              <Form.Item
                name="decided_by"
                label="审批人"
                rules={[{ required: true, message: '请输入审批人' }]}
              >
                <Input placeholder="审批人 (decided_by)" />
              </Form.Item>
              <Form.Item name="comment" label="备注">
                <Input.TextArea rows={2} placeholder="备注 (comment)" />
              </Form.Item>
            </>
          )}

          {action?.kind === 'transfer' && (
            <>
              <Form.Item
                name="new_approver"
                label="新审批人"
                rules={[{ required: true, message: '请输入新审批人' }]}
              >
                <Input placeholder="新审批人 (new_approver)" />
              </Form.Item>
              <Form.Item name="reason" label="原因">
                <Input.TextArea rows={2} placeholder="原因 (reason)" />
              </Form.Item>
            </>
          )}

          {action?.kind === 'escalate' && (
            <>
              <Form.Item
                name="escalated_to"
                label="升级给"
                rules={[{ required: true, message: '请输入升级对象' }]}
              >
                <Input placeholder="升级给 (escalated_to)" />
              </Form.Item>
              <Form.Item name="reason" label="原因">
                <Input.TextArea rows={2} placeholder="原因 (reason)" />
              </Form.Item>
            </>
          )}
        </Form>
      </Modal>
    </div>
  )
}
