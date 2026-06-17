# ingestion-worker

独立 FastAPI 进程，负责按 mime_type 解析文档、段落切分、调用 EmbeddingProvider、回写 knowledge-api。

## 端点

- `POST /internal/parse` — 输入 `{doc_id, file_path, mime_type, metadata}`，输出 `{chunks, embeddings, embedding_model}`
- `GET /health` — 报告状态与 embedding 提供方

## 解析器

| mime_type | 解析器 |
|---|---|
| `text/markdown` | MarkdownParser（标题层级 → section_path） |
| `text/html` | HtmlParser（h1/h2/h3 切片，去标签） |
| `text/plain` | TextParser（按空行分段） |
| 其他 | `mime_unsupported` 415 |

## EmbeddingProvider

- `stub`（默认，零依赖，确定性 hash 向量，dim=8）
- `openai_compat`（OpenAI-compatible 远程接口；配置缺失自动降级 stub）

## 本地启动

```bash
conda activate ai-employee
EMBEDDING_PROVIDER=stub \
EMBEDDING_DIM=8 \
KNOWLEDGE_API_INTERNAL_TOKEN=dev-token \
uvicorn ai_employee.ingestion_worker.app:app --port 8001 --app-dir services/ingestion-worker/src
```
