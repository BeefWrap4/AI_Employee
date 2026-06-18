# knowledge-api

FastAPI service for RAG query, hybrid retrieval (FTS5 + vector recall), document lifecycle (6-state machine), citation, feedback, and qa_log persistence to SQLite.

## 端点

- `POST /api/v1/documents` — multipart 上传文件，触发 ingestion-worker 解析，返回 `doc_id`、`parse_status`、`worker_dispatch`
- `GET /api/v1/documents/{doc_id}` — 查询文档状态
- `GET /api/v1/documents/{doc_id}/chunks` — 列出文档分段
- `POST /api/v1/documents/{doc_id}/publish` — 发布（仅 `ready` 可发布）
- `POST /api/v1/documents/{doc_id}/reparse` — 失败重试（仅 `parse_failed`）
- `POST /api/v1/documents/{doc_id}/archive` / `/restore` — 归档与恢复
- `POST /api/v1/chat/query` — 带引用问答
- `POST /api/v1/feedback` — 用户反馈
- `GET /health`
- 内部端点 `POST /internal/chunks`、`POST /internal/documents/{doc_id}/parse-failed`（共享 token 鉴权）

## 状态机

`uploaded → parsing → ready → published → archived`，失败侧 `parsing → parse_failed →（reparse）uploaded`。

## 本地启动

```bash
conda activate ai-employee
# 先启动 worker（见 services/ingestion-worker）
KNOWLEDGE_API_INTERNAL_TOKEN=dev-token \
KNOWLEDGE_DATA_DIR=./var/data \
INGESTION_WORKER_URL=http://127.0.0.1:8001 \
uvicorn ai_employee.knowledge_api.app:app --port 8010 --app-dir services/knowledge-api/src
```
