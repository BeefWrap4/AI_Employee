# Spec — R22 收尾：MinIO 对象存储抽象

日期：2026-06-19
主题：R22 阶段交付总览——ObjectStore 抽象（LocalFs / S3 / MinIO 三后端）、knowledge-api 与 agent-platform-api 接线、k8s/Helm 部署清单
适用范围：`packages/object-store`、`services/knowledge-api`、`services/agent-platform-api`、`infra/k8s`、`infra/helm`、`tests/`

## 1. 目标 (Goal)

R21 之前，大体积二进制对象（上传 PDF / 图片、审批补充附件 `attachments`、解析后文档备份）一律落在单节点本地卷 `./var/uploads/` 或 `./var/data/raw/`，带来三个问题：

1. **水平扩展受阻**：knowledge-api / agent-platform-api 副本数 >1 时，上传落在 Pod A 的本地卷，发布/补充流程落在 Pod B 时读不到字节。
2. **附件耦合 base64**：R20-1 审批补充流程（`request_supplement` / `resolve_supplement`）的 `attachments` 只能内联 `uri`，无法引用一份已上传对象，reviewer 也拿不到独立下载链接。
3. **生产对象存储缺位**：没有 S3 / MinIO 这一层，灾备 / 冷归档 / 跨可用区读取都无从谈起。

R22 引入一个 **极薄** 的 `ObjectStore` 协议，让同一份业务代码在 dev/test 跑本地文件系统、在 prod 跑 S3 / MinIO，且不重写调用方。具体目标：

1. 新增 `packages/object-store`：`ObjectStore` Protocol + `LocalFsObjectStore`（默认，零依赖）+ `S3ObjectStore`（boto3，懒加载）+ `MinioObjectStore` 别名 + `build_object_store()` 工厂。
2. 把 knowledge-api 上传走写穿（write-through）对象存储、agent-platform-api 新增 `POST /api/v1/objects` 上传与 `GET /api/v1/objects/{key}/download` 下载端点，并把审批补充 `attachments` 升级为可携带 `object_key`。
3. 给出 k8s 原生 manifest + Helm 模板，让 MinIO 可以一键起在 `ai-employee` namespace，并通过 `values.yaml.objectStore` 切换 LocalFs / MinIO。

## 2. 实现清单 (Deliverables)

### 2.1 `packages/object-store`（新包）

| 文件 | 变更要点 |
| --- | --- |
| `src/ai_employee/object_store/__init__.py` (新) | 定义 `@runtime_checkable ObjectStore` Protocol（`put` / `get` / `exists` / `delete` / `presign` / `get_metadata`）；`LocalFsObjectStore`（metadata 以 `{key}.meta.json` sidecar 落盘，`presign` 生成同源 `/api/v1/objects/{key}` URL）；`build_object_store()` 工厂按 `OBJECT_STORE_URL` 是否设置选 LocalFs / S3；`_validate_key()` 拒绝空 key、绝对路径、反斜杠、`..` 段，防目录穿越。 |
| `src/ai_employee/object_store/s3.py` (新) | `S3ObjectStore`：boto3 客户端懒加载（`__init__` 内 import，LocalFs 环境无需装 boto3）；`signature_version="s3v4"`；`get` / `exists` / `get_metadata` 兼容 `NoSuchKey` / `404` / `NotFound` 多种 boto 错误码；`presign` 走 `generate_presigned_url`。`MinioObjectStore(S3ObjectStore)` 别名：接受 `secure` 标志并把 `http://` 规整成 `https://`，仅表达意图、复用同一 S3 代码路径。 |
| `README.md` (新) | 后端选择矩阵、env 配置示例、API 速查、向后兼容说明（LocalFs 写穿仍保留原始磁盘布局，避免破坏既有 ingestion 测试）。 |
| `pyproject.toml` (根) | `boto3>=1.34` 进 `dependencies`；`ai_employee.object_store` 进 `tool.setuptools.packages` 与 `package-dir` 映射；`pythonpath` 增 `packages/object-store/src`。 |
| `pytest.ini` | `pythonpath` 增 `packages/object-store/src`。 |

### 2.2 服务接线（r22-2）

| 模块 | 文件 | 变更要点 |
| --- | --- | --- |
| `services/agent-platform-api` | `src/ai_employee/agent_platform_api/app.py` | 新增 `POST /api/v1/objects`：multipart 上传，key 形如 `{prefix}/{tenant}/{uuid}{ext}`（`prefix=OBJECT_STORE_PREFIX` 默认 `uploads`，`tenant=TENANT_ID` 默认 `default`），写穿后返回 `{object_key, size, content_type, presigned_url}`；空对象 400、非法 key 400。新增 `GET /api/v1/objects/{key:path}/download`：走 `get_metadata` + `get` 流式回吐，缺失 404 `{"error_code":"object_not_found"}`。`request_supplement` / `resolve_supplement` 改为先 `normalize_attachments()` 再落库，失败 400 `invalid_attachment`。 |
| `services/agent-platform-api` | `src/ai_employee/agent_platform_api/object_refs.py` (新) | `normalize_attachment()`：`object_key` 有而 `uri` 空时，用 `store.presign()` 补出下载 URL（LocalFs → 同源 `/api/v1/objects/{key}`，S3 → 真预签名 URL）；两者皆空 400；`presign` 抛 `KeyError` 时保留 `object_key`、`uri=None`（对象尚未上传的过渡态）。 |
| `services/agent-platform-api` | `src/ai_employee/agent_platform_api/schemas.py` | `SupplementAttachment` 增 `object_key: str | None`、`size: int | None` 等字段，向后兼容旧 `uri`-only 形态。 |
| `services/knowledge-api` | `src/ai_employee/knowledge_api/app.py` | `POST /api/v1/knowledge/upload` 在写本地 `raw_dir` 之前**写穿对象存储**：`obj_key = documents/{uuid}.{ext}`，`store.put(..., metadata={"title": title})`；写入失败仅 warning + `obj_key=None`（best-effort，不阻断上传）；成功时把 `object_key` 并进文档 `metadata`，发布流程后续可凭 key 回 S3 / MinIO 取字节而无需重传。本地磁盘写入保留，ingestion-worker 仍按 path 读（向后兼容）。 |

### 2.3 部署清单（r22-4）

| 文件 | 变更要点 |
| --- | --- |
| `infra/k8s/minio.yaml` (新) | 单节点单盘 MinIO StatefulSet：`minio/minio:RELEASE.2025-04-22T22-12-26Z`；启动命令 `mkdir -p /data/ai-employee && minio server /data --console-address ":9001"`；`MINIO_ROOT_USER/PASSWORD` 走 `minio-credentials` Secret（`optional: true`，方便 Helm 接管）；readiness `/minio/health/ready`、liveness `/minio/health/live`；资源 `100m/256Mi` → `2000m/1Gi`；20Gi PVC（`storageClassName` 留空走集群默认）。附独立 `Secret` 占位（`REPLACE_WITH_*`）。 |
| `infra/helm/templates/minio.yaml` (新) | `{{- if .Values.objectStore.minio.enabled }}` 门控的 Helm 模板：同结构 Secret + PVC + Service + StatefulSet，`storageClassName` 取 `global.storageClassName`，labels 复用 `include "ai-employee.labels"`。 |
| `infra/helm/templates/deployment.yaml` | 每个服务 Pod 注入 `OBJECT_STORE_URL` / `OBJECT_STORE_LOCAL_ROOT` / `OBJECT_STORE_ACCESS_KEY` / `OBJECT_STORE_SECRET_KEY` / `OBJECT_STORE_BUCKET`，全部来自 `.Values.objectStore`，默认值与 LocalFs 对齐。 |
| `infra/helm/values.yaml` | 新增顶层 `objectStore` 块：`url: ""`（空=LocalFs）/ `accessKey` / `secretKey` / `bucket: ai-employee` / `localRoot: /var/lib/ai-employee/objects`；`minio.enabled/storage/rootUser/rootPassword/consolePort` 子块（默认 `enabled: false`）。 |

### 2.4 测试

| 测试文件 | 用例数 | 覆盖点 |
| --- | --- | --- |
| `tests/test_object_store.py` (新) | 14 | LocalFs put/get/exists/delete/presign/metadata 往返；绝对路径与 `..` 穿越被拒；`build_object_store` 默认 LocalFs、设 `OBJECT_STORE_URL` 切 S3；`S3ObjectStore` 经 moto 跑 put/get/exists/delete/presign/get_metadata 全路径；`MinioObjectStore` 别名构造 + `secure` 规整；Protocol `isinstance` 静态满足。 |
| `tests/test_agent_platform_objects_api.py` (新) | 4 | `POST /api/v1/objects` 上传返回 `object_key`+`presigned_url`+`size`；空对象 400；`GET /api/v1/objects/{key}/download` 流式回吐正确 content-type；缺失对象 404。 |
| `tests/test_agent_platform_supplement_object_key.py` (新) | 3 | `normalize_attachment` 用 `object_key` 补出 `uri`；`object_key` 与 `uri` 皆空抛 `ValueError`；补充治理端点 `request_supplement` 携带 `object_key` 附件成功落库且 `attachments[*].uri` 非空。 |
| `tests/test_knowledge_api_object_store.py` (新) | 1 | upload 写穿对象存储后，文档 `metadata.object_key` 非空且 `store.get(key)` 字节与上传一致；对象存储失败时降级本地仍成功。 |
| `tests/test_helm_templates.py` (新) | 15 | `helm template` 渲染 minio StatefulSet/PVC/Service/Secret 结构正确；`objectStore.minio.enabled=false` 时不渲染 minio 资源；服务 Pod 注入 5 个 `OBJECT_STORE_*` env；`storageClassName` 透传；`url` 为空时 LocalFs 路径生效；labels / namespace 正确。 |

合计 +37 个新断言（5 个新测试文件），全部走 `TestClient` + `moto`（S3 mock）+ `helm template` 子进程，无需真实 MinIO / S3。

### 2.5 ruff 收尾（r22-style）

`59e6e0d` 把懒导入注释化（`# lazy import`）、把 `lambda` 提为 `Callable` 类型字段 `_client_factory`、用 `dict()` 推导替换手写循环，消除 ruff `RUF059` / `E731` / 风格告警，无行为变化。

## 3. 测试结果 (Test Results)

- 全量 R16–R21 测试保持通过（`test_rca_report_eval`、`test_safety_policy_eval`、`test_tool_call_correctness`、`test_multiturn_context.py`、approval 状态机回归等无回归）。
- 新增 5 个 R22 测试文件在 `pytest -q tests/test_object_store.py tests/test_agent_platform_objects_api.py tests/test_agent_platform_supplement_object_key.py tests/test_knowledge_api_object_store.py tests/test_helm_templates.py` 下全部通过；`test_object_store.py` 的 S3 路径由 `moto` mock，CI 无需起 MinIO。
- `helm template infra/helm/ -f infra/helm/values.yaml` 渲染成功；切换 `objectStore.minio.enabled=true` 后 minio StatefulSet + PVC + Service + Secret 四类资源齐备。
- 静态检查：`ruff check` / `mypy --strict` 在改动文件上无新增告警（`59e6e0d` 已收尾）。
- 端到端手测（curl + LocalFs）：`POST /api/v1/objects` 上传 PDF → 返回 `object_key=uploads/default/<uuid>.pdf` + `presigned_url=/api/v1/objects/uploads/default/<uuid>.pdf`；`GET` 该 URL 流式回吐字节、`Content-Type` 透传；`request_supplement` 带 `object_key` 附件落库后 `attachments[0].uri` 自动补出下载链接。

## 4. 已知遗留 (Known Gaps)

- **LocalFs `presign` 非真签名**：LocalFs 后端只生成同源 `/api/v1/objects/{key}` URL，鉴权完全依赖 agent-platform-api 自身的 `X-Internal-Token` / JWT 链；S3 / MinIO 后端才是真预签名。跨后端语义不一致，前端需感知后端类型。
- **knowledge-api 写穿是 best-effort**：对象存储写入失败仅 warning + 降级本地，`metadata.object_key` 留空；极端情况下发布流程若依赖该 key 取字节会落空，需调用方回退读本地 `raw_dir`。无重试 / 无死信。
- **`OBJECT_STORE_PREFIX` / `TENANT_ID` 无强校验**：`POST /api/v1/objects` 的 key 前缀取自 env，多租户隔离仅靠 `TENANT_ID` 字符串拼接，未做 ACL；跨租户 `object_key` 下载端点不做归属校验（与 M2.2 ACL 设计一致，待合并）。
- **MinIO 单节点单盘**：k8s manifest 是 StatefulSet + 单 PVC，无 erasure coding / 无分布式集群；生产前需替换为 MinIO Distributed 模式或托管 S3。`volumeClaimTemplates: []` 留空，数据卷实际靠独立 PVC `minio-data` 挂载。
- **无迁移脚本**：既有 `./var/uploads/` / `./var/data/raw/` 历史对象未迁移到对象存储；新上传才走写穿。历史对象在 LocalFs 后端下仍可读，但切到 S3 / MinIO 后旧路径不可达。
- **下载端点无 Range / 无限流**：`GET /api/v1/objects/{key}/download` 一次性 `store.get()` 读全量字节再 `Response`，大对象会常驻内存；未支持 HTTP Range / 分块。
- **`MinioObjectStore.secure` 规整不完整**：`secure=True` 且 `endpoint_url` 已是 `https://` 时无处理；`secure=False` 且 `https://` 时也不会降级（注释标注为「接受 MinIO 文档约定」，未覆盖所有组合）。

## 5. 下一步建议 (R23 Candidates)

按「价值/工作量」排序的后续候选：

1. **下载端点 Range + 分块流**（高价值，低工作量）—— `GET /api/v1/objects/{key}/download` 改 `StreamingResponse`，S3 后端走 `get_object(Range=...)`，LocalFs 走分块读，支持大 PDF / 视频回放。
2. **对象存储写穿强一致 + 重试**（高价值，中工作量）—— knowledge-api 写穿失败改为带退避重试 + 死信队列，`metadata.object_key` 在确认持久化后才落库，消除 best-effort 窗口。
3. **对象 key ACL + 租户隔离**（高价值，中工作量）—— 复用 M2.2 ACL 中间件，下载端点按 `TENANT_ID` + key 前缀做归属校验，跨租户 403。
4. **历史对象迁移脚本**（中价值，低工作量）—— `scripts/migrate_local_to_object_store.py` 扫 `./var/data/raw/` 与 `./var/uploads/`，按 `doc_id` / 上传时间批量 `put` 到对象存储并回填 `metadata.object_key`。
5. **MinIO 分布式 / 托管 S3 生产化**（高价值，中工作量）—— k8s manifest 替换为 MinIO Distributed（≥4 节点 erasure coding）或切 AWS S3 + IRSA，Helm values 增 `objectStore.provider: s3|minio-distributed` 分支。
6. **对象生命周期 + 冷归档**（探索性）—— 基于 `metadata` 的 `created_at` 做 S3 Lifecycle 规则，超 N 天的审批附件转 Glacier，下载端点感知存储类并返回 `202 + Restore` 提示。

建议 R23 主线接 1+2+3，把对象存储从「能跑」推到「生产可依赖」，再开 4/5/6 做存量治理与成本优化。
