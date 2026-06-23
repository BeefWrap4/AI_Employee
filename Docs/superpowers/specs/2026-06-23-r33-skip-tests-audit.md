# R33-E: pytest skip / skipif / importorskip audit

**Date:** 2026-06-23
**Round:** R33-E
**Scope:** Every `pytest.skip`, `@pytest.mark.skip`, `@pytest.mark.skipif`, and `pytest.importorskip` occurrence under `tests/`.
**Method:** `grep -rn "pytest\.skip\|@pytest\.mark\.skip\|@pytest\.mark\.skipif\|pytest\.xfail\|importorskip"` over `tests/`, then manual read of each hit's surrounding context.

## Summary

The grep found **zero** `pytest.xfail` / `@pytest.mark.xfail` usages and **zero** unconditional `pytest.skip()` calls (every skip is gated on an env var, a platform check, a `shutil.which()` capability probe, or an optional-dependency import). Every skip is a legitimate env/capability gate; no skip masks a real test failure. Kafka alarm-consumer tests do **not** require a live broker — they inject `FakeKafkaConsumer` — so there is no "Kafka-live" skip category.

Two skip *mechanisms* are in use:

1. **`pytest.skip()` inside the test body** — runtime skip after a capability/env probe (e.g. `shutil.which("helm")`, `backend.available`, `sys.platform`).
2. **`@pytest.mark.skipif(...)` decorator** — declarative skip on a condition evaluated at collection time (env-var unset).
3. **`pytest.importorskip("dep")`** — skip the whole module/function when an optional dependency is absent.

## Catalogue

Grouped by category. "CI un-skips when" = the environment that makes the test actually run (and is expected to pass).

### Postgres-live (`TEST_POSTGRES_URL`)

| File | Line | Mechanism | Condition / gate | CI un-skips when | Legitimate |
|---|---|---|---|---|---|
| `tests/test_db_abstraction.py` | 190 | `pytest.skip()` | `TEST_POSTGRES_URL` deleted via monkeypatch (meta-test asserting the skip itself fires) | n/a — this test *always* skips by design; it pins the skip behaviour | yes — it is a passing meta-test, not a masked failure |
| `tests/test_db_abstraction.py` | 193 | `@pytest.mark.skipif` | `not os.getenv("TEST_POSTGRES_URL")` | `TEST_POSTGRES_URL` set (Linux CI PG job) | yes — live PG round-trip, needs a real DB |
| `tests/test_knowledge_store_dual_backend.py` | 97 | `@pytest.mark.skipif` | `not os.getenv("TEST_POSTGRES_URL")` | `TEST_POSTGRES_URL` set | yes — `build_knowledge_store` PG path |
| `tests/test_knowledge_store_dual_backend.py` | 290 | `@pytest.mark.skipif` | `not os.getenv("TEST_POSTGRES_URL")` | `TEST_POSTGRES_URL` set | yes — `PgKnowledgeStore` live round-trip |
| `tests/test_platform_run_store_dual_backend.py` | 65 | `@pytest.mark.skipif` | `not os.getenv("TEST_POSTGRES_URL")` | `TEST_POSTGRES_URL` set | yes — `build_run_store` PG path |
| `tests/test_platform_run_store_dual_backend.py` | 156 | `@pytest.mark.skipif` | `not os.getenv("TEST_POSTGRES_URL")` | `TEST_POSTGRES_URL` set | yes — `PgAgentRunStore` live round-trip |
| `tests/test_rca_platform_dual_backend.py` | 113 | `@pytest.mark.skipif` | `not os.getenv("TEST_POSTGRES_URL")` | `TEST_POSTGRES_URL` set | yes — `build_rca_store` PG path |
| `tests/test_rca_platform_dual_backend.py` | 211 | `@pytest.mark.skipif` | `not os.getenv("TEST_POSTGRES_URL")` | `TEST_POSTGRES_URL` set | yes — `PgRcaStore` live round-trip |
| `tests/test_migration_postgres_compat.py` | 200 | `@pytest.mark.skipif` | `not os.getenv("TEST_POSTGRES_URL")` | `TEST_POSTGRES_URL` set | yes — Alembic baseline applies to live PG |
| `tests/test_storage_backend.py` | 103 | `pytest.importorskip` | `psycopg` not installed | `psycopg` installed | yes — PG backend needs the driver |
| `tests/test_storage_backend.py` | 117 | `pytest.skip()` (in `pg_backend` fixture) | `TEST_POSTGRES_URL` and `POSTGRES_TEST_DSN` both unset | either DSN env set | yes — fixture feeds live-PG tests |

### Windows-WAL

| File | Line | Mechanism | Condition / gate | CI un-skips when | Legitimate |
|---|---|---|---|---|---|
| `tests/test_rca_tool_call_log.py` | 144 | `pytest.skip()` | `sys.platform.startswith("win")` | run on Linux/macOS CI (default) | yes — documented R30-C Windows SQLite WAL visibility quirk; same write/read contract is pinned cross-platform by the direct unit test `test_tool_call_log_records_per_adapter_call` |

### CLI-tool (`shutil.which` capability probe)

| File | Line | Mechanism | Condition / gate | CI un-skips when | Legitimate |
|---|---|---|---|---|---|
| `tests/test_helm_templates.py` | 34 | `pytest.skip()` (fixture) | `helm` CLI not on PATH | helm installed (Linux CI) | yes — live `helm template` render |
| `tests/test_helm_templates.py` | 234 | `pytest.skip()` (fixture) | `helm` CLI not on PATH | helm installed | yes — object-store overlay render |
| `tests/test_helm_templates.py` | 258 | `pytest.skip()` (fixture) | `helm` CLI not on PATH | helm installed | yes — MinIO StatefulSet render |
| `tests/test_r29_pg_defaulting.py` | 461 | `pytest.skip()` (fixture) | `helm` CLI not on PATH | helm installed | yes — default chart DATABASE_URL render |
| `tests/test_r29_pg_defaulting.py` | 504 | `pytest.skip()` | `helm` CLI not on PATH | helm installed | yes — DATABASE_URL override render |
| `tests/test_local_ci.py` | 53 | `pytest.skip()` | `ruff` not on PATH | ruff installed | yes — ruff lint gate |
| `tests/test_local_ci.py` | 66 | `pytest.skip()` | `ruff` not on PATH | ruff installed | yes — ruff format check |
| `tests/test_local_ci.py` | 79 | `pytest.skip()` | `bandit` not on PATH | bandit installed | yes — security scan gate |
| `tests/test_local_ci.py` | 91 | `pytest.skip()` | `npm` not on PATH | npm + pnpm installed (Linux CI) | yes — frontend Vitest run |
| `tests/test_local_ci.py` | 106 | `pytest.skip()` | `helm` not on PATH | helm installed | yes — helm template render |

### OCR-backend (availability probe)

| File | Line | Mechanism | Condition / gate | CI un-skips when | Legitimate |
|---|---|---|---|---|---|
| `tests/test_ocr_parser.py` | 173 | `pytest.skip()` | `RapidOcrBackend.available` is True (rapidocr IS installed) | rapidocr NOT installed | yes — tests the degrade-to-empty path, only meaningful when the dep is absent |
| `tests/test_ocr_parser.py` | 182 | `pytest.skip()` | `TesseractOcrBackend.available` is True (tesseract IS installed) | tesseract NOT installed | yes — same reason for the tesseract backend |

### Optional-dependency (`pytest.importorskip`)

These skip a module/function when an optional extra is absent. They are capability gates, not failure masks.

| File | Line | Gate dep | CI un-skips when | Legitimate |
|---|---|---|---|---|
| `tests/test_helm_templates.py` | 21 | `yaml` (PyYAML) | PyYAML installed | yes |
| `tests/test_migration_postgres_compat.py` | 22 | `alembic` | alembic installed | yes |
| `tests/test_migrations.py` | 15 | `alembic` | alembic installed | yes |
| `tests/test_migrate_postgres_script.py` | 10 | `alembic` | alembic installed | yes |
| `tests/test_object_store.py` | 115, 177 | `moto` | moto installed | yes — S3 mock |
| `tests/test_object_store.py` | 116, 224, 264 | `boto3` | boto3 installed | yes — S3/MinIO backend |
| `tests/test_prometheus_rules.py` | 9 | `yaml` | PyYAML installed | yes |
| `tests/test_r29_pg_defaulting.py` | 476, 505 | `yaml` | PyYAML installed | yes |
| `tests/test_r33f_helm_prod.py` | 29 | `yaml` | PyYAML installed | yes |
| `tests/test_r33g_observability.py` | 22 | `yaml` | PyYAML installed | yes |
| `tests/test_redis_event_bus.py` | 281, 317 | `fakeredis` | fakeredis installed | yes — Redis pub/sub mock |
| `tests/test_storage_backend.py` | 103 | `psycopg` | psycopg installed | yes (listed under Postgres-live above) |

### Checkpointer optional extras (`langgraph-checkpoint-{redis,postgres}`)

| File | Line | Mechanism | Condition / gate | CI un-skips when | Legitimate |
|---|---|---|---|---|---|
| `tests/test_r33a_checkpointer_factory.py` | 81 | `pytest.skip()` | `langgraph-checkpoint-redis` not importable | extra installed | yes — asserts `RedisSaver` is returned when the extra is present |
| `tests/test_r33a_checkpointer_factory.py` | 99 | `pytest.skip()` | `langgraph-checkpoint-redis` IS importable | extra NOT installed | yes — complementary: asserts degrade-to-`MemorySaver` when the extra is absent |
| `tests/test_r33a_checkpointer_factory.py` | 124 | `pytest.skip()` | `langgraph-checkpoint-postgres` not importable | extra installed | yes — asserts `PostgresSaver` returned when present |
| `tests/test_r33a_checkpointer_factory.py` | 137 | `pytest.skip()` | `langgraph-checkpoint-postgres` IS importable | extra NOT installed | yes — complementary degrade-path assertion |

These four form two mutually-exclusive pairs: for each backend, exactly one of the "available" / "degrade" tests runs in any given environment. Together they fully cover both branches.

### Platform / filesystem

| File | Line | Mechanism | Condition / gate | CI un-skips when | Legitimate |
|---|---|---|---|---|---|
| `tests/test_path_guard.py` | 65 | `pytest.skip()` | `symlink_to()` raises `OSError`/`NotImplementedError` | platform supports symlinks (Linux/macOS CI) | yes — symlink-escape guard only testable where symlinks exist |

### Kafka (no live-broker skip)

The grep for `KAFKA`/`kafka` in `tests/` returns hits in `tests/test_kafka_ingest.py`, `tests/test_event_gateway.py`, and `tests/test_r27_kafka_neo4j_scoring.py`. **None of these skip on a live broker.** They all inject `FakeKafkaConsumer` (or stub `_connect_kafka`) so no Kafka broker is required. `test_event_gateway.py:258` even asserts that `rca-agent` no longer gates on `KAFKA_ENABLED`. There is therefore no "Kafka-live" skip category to catalog; Kafka is fully stubbed in the suite.

## Concerns

**None.** No skip was found that masks a real failure:

- No `pytest.xfail` / `@pytest.mark.xfail` markers exist anywhere in `tests/` (a common failure-masking vector).
- No unconditional `pytest.skip()` (with no preceding capability/env check) was found. The one `pytest.skip()` in `test_db_abstraction.py:190` is *inside* a test whose explicit purpose is to assert that the skip fires when `TEST_POSTGRES_URL` is unset — it is a passing meta-test, not a masked failure.
- Every `pytest.importorskip` targets a genuinely optional dependency (`yaml`, `alembic`, `psycopg`, `boto3`, `moto`, `fakeredis`) — all of which are present in the canonical `environment.yml` dev install, so they un-skip in normal CI.
- The Windows-WAL skip (`test_rca_tool_call_log.py:144`) is documented in detail and has a cross-platform unit-test backup (`test_tool_call_log_records_per_adapter_call`) that pins the same contract on a single connection, so the regression coverage is preserved on Windows.
- The OCR and checkpointer "available-path" skips are intentionally complementary to their "degrade-path" siblings — exactly one of each pair runs per environment, giving full branch coverage across CI matrixes.

No production test logic was modified during this audit.

## CI operator cheat-sheet

| Env / capability you provide | Skips that un-skip |
|---|---|
| `TEST_POSTGRES_URL=postgresql://...` | 9 live-PG `skipif` tests + `pg_backend` fixture-fed tests (db_abstraction, dual-backend stores, migration_compat, storage_backend) |
| `psycopg` installed | `test_storage_backend.py` PG section |
| `alembic` installed | migration test modules |
| `boto3` + `moto` installed | `test_object_store.py` S3/MinIO tests |
| `fakeredis` installed | `test_redis_event_bus.py` pub/sub tests |
| `langgraph-checkpoint-redis` installed | `test_r33a_checkpointer_factory.py:81` (available path) |
| `langgraph-checkpoint-postgres` installed | `test_r33a_checkpointer_factory.py:124` (available path) |
| `helm` on PATH | `test_helm_templates.py` (×3), `test_r29_pg_defaulting.py` (×2), `test_local_ci.py::test_helm_template_renders` |
| `ruff` on PATH | `test_local_ci.py` ruff lint + format |
| `bandit` on PATH | `test_local_ci.py::test_bandit_passes` |
| `npm` on PATH | `test_local_ci.py::test_frontend_vitest_passes` |
| run on Linux/macOS | `test_rca_tool_call_log.py::test_e2e_run_records_in_tool_call_log` (Windows-WAL skip avoided) |
| platform supports symlinks | `test_path_guard.py::test_symlink_escaping_raw_rejected` |
| rapidocr / tesseract NOT installed | the two OCR degrade-path tests |

The default Linux CI image (per `environment.yml` + `test_local_ci.py` running on Linux) provides every Python dependency, `ruff`, `bandit`, `helm`, `npm`, and a real Postgres via `TEST_POSTGRES_URL`, so the only skips that persist in default CI are the OCR "available-path" tests (when rapidocr/tesseract ARE installed) and the checkpointer "available-path" tests for whichever extras are installed — i.e. the intentionally complementary branches.
