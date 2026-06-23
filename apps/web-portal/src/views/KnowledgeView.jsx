import React, { useEffect, useState } from 'react'
import {
  Button,
  Card,
  Input,
  List,
  Space,
  Tag,
  Typography,
  Upload,
  message,
} from 'antd'
import { UploadOutlined } from '@ant-design/icons'
import { knowledgeApi } from '../api.js'

// Knowledge base view: list documents and run a query against the
// knowledge-api chat/query endpoint to surface cited evidence.
export default function KnowledgeView() {
  const [docs, setDocs] = useState([])
  const [question, setQuestion] = useState('')
  const [answer, setAnswer] = useState(null)
  const [loading, setLoading] = useState(false)
  const [uploadTitle, setUploadTitle] = useState('')
  const [uploading, setUploading] = useState(false)

  const loadDocs = async () => {
    try {
      const data = await knowledgeApi.listDocuments({ page: 1, page_size: 50 })
      setDocs(data.items || [])
    } catch (e) {
      message.error(`加载文档失败: ${e.message}`)
    }
  }

  useEffect(() => {
    loadDocs()
  }, [])

  const runQuery = async () => {
    if (!question.trim()) return
    setLoading(true)
    try {
      const result = await knowledgeApi.query(question, [])
      setAnswer(result)
    } catch (e) {
      message.error(`查询失败: ${e.message}`)
      setAnswer(null)
    } finally {
      setLoading(false)
    }
  }

  // antd Upload hands us the selected file via beforeUpload; returning
  // false prevents its own XHR so we can POST the multipart body through
  // knowledgeApi.uploadDocument (which sets auth + proxy prefix).
  const handleUpload = async (file) => {
    if (!uploadTitle.trim()) {
      message.warning('请输入文档标题')
      return false
    }
    setUploading(true)
    try {
      await knowledgeApi.uploadDocument(file, uploadTitle.trim(), {}, [])
      message.success(`文档已上传: ${uploadTitle.trim()}`)
      setUploadTitle('')
      await loadDocs()
    } catch (e) {
      message.error(`上传失败: ${e.message}`)
    } finally {
      setUploading(false)
    }
    return false
  }

  return (
    <div>
      <h2 className="page-title">知识库</h2>
      <Space.Compact style={{ width: '100%', marginBottom: 16 }}>
        <Input
          placeholder="输入运维问题，例如：5G 小区 RRC 建立失败率升高怎么排查？"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onPressEnter={runQuery}
        />
        <Button type="primary" onClick={runQuery} loading={loading}>
          检索问答
        </Button>
      </Space.Compact>

      {answer && (
        <Card title="问答结果" size="small" style={{ marginBottom: 16 }}>
          <Typography.Paragraph>{answer.answer || answer.summary || JSON.stringify(answer)}</Typography.Paragraph>
          {answer.citations && answer.citations.length > 0 && (
            <List
              size="small"
              header={<Typography.Text strong>引用证据</Typography.Text>}
              dataSource={answer.citations}
              renderItem={(c) => (
                <List.Item>
                  <Typography.Text>
                    [{c.doc_id}] {c.section_path || ''} — {c.content?.slice(0, 120)}
                  </Typography.Text>
                </List.Item>
              )}
            />
          )}
        </Card>
      )}

      <Card title="上传文档" size="small" style={{ marginBottom: 16 }}>
        <Space.Compact style={{ width: '100%' }}>
          <Input
            placeholder="文档标题"
            value={uploadTitle}
            onChange={(e) => setUploadTitle(e.target.value)}
            style={{ maxWidth: 320 }}
          />
          <Upload
            beforeUpload={handleUpload}
            showUploadList={false}
            accept=".pdf,.docx,.xlsx,.md,.txt"
            disabled={uploading}
          >
            <Button icon={<UploadOutlined />} loading={uploading}>
              上传文档
            </Button>
          </Upload>
        </Space.Compact>
      </Card>

      <Card title="文档列表" size="small">
        <List
          size="small"
          dataSource={docs}
          renderItem={(d) => (
            <List.Item>
              <List.Item.Meta
                title={d.title}
                description={
                  <Space>
                    <Tag color="blue">{d.status}</Tag>
                    <span>{d.mime_type}</span>
                    {d.scope && <Tag>{d.scope}</Tag>}
                  </Space>
                }
              />
            </List.Item>
          )}
          locale={{ emptyText: '暂无文档' }}
        />
      </Card>
    </div>
  )
}
