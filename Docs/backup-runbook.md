# Backup Runbook (R30-C)

> **Spec ref:** `docs/project-3-intelligent-ops-agent-platform-design-spec.md` §9 — *高可用设计 / 关键数据定期备份* and `docs/ai-agent-telecom-projects-implementation-plan.md` §9 — 后续开发拆分原则.
> **Owner:** SRE on-call
> **Last reviewed:** 2026-06-22 (R30-C)

This runbook documents the periodic backup strategy for the AI Employee stack's
three stateful subsystems (Postgres, MinIO, Redis) and the operator playbook to
restore them. It complements the live-deployment `infra/helm/` manifests and the
local dev `docker-compose.yml`.

## 1. Scope & RPO/RTO targets

| Subsystem | Data class | RPO (max data loss) | RTO (max recovery time) | Backup cadence |
|---|---|---|---|---|
| **PostgreSQL** | Agent runs, approvals, knowledge, RCA incidents | ≤ 1 h | ≤ 2 h | Daily full + WAL every 15 min |
| **MinIO** (S3) | Knowledge raw files, approval-supplement attachments, RCA reports | ≤ 24 h | ≤ 4 h | Daily mirror to a second bucket |
| **Redis** | Event bus (in-flight events), rate-limit counters, dedup sets | ≤ 1 h (best-effort) | ≤ 30 min (rebuild from PG events) | Hourly BGSAVE |

> "Best-effort" on Redis is acceptable because the agent-platform emits all
> durable events to PG (R29-C event-gateway); the Redis bus is a
> fast-path accelerator only. A cold Redis is rebuilt from the
> `event_outbox` table.

## 2. PostgreSQL — `pg_dump` + WAL archiving

### 2.1 Daily logical backup

```bash
# DATABASE_URL is the connection string for the primary.
# Output: timestamped SQL dump, gzipped, ~7-day retention.
TS=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR=/var/backups/ai-employee/pg
mkdir -p "$BACKUP_DIR"

pg_dump "$DATABASE_URL" \
    --format=custom \
    --compress=9 \
    --no-owner \
    --no-privileges \
    --file="$BACKUP_DIR/ai-employee-${TS}.dump"

# Prune > 7 days.
find "$BACKUP_DIR" -name 'ai-employee-*.dump' -mtime +7 -delete
```

Restore:

```bash
pg_restore --clean --if-exists --dbname "$DATABASE_URL" \
    /var/backups/ai-employee/pg/ai-employee-20260622T020000Z.dump
```

### 2.2 WAL streaming (continuous, every 15 min)

Configure `postgresql.conf` on the primary:

```ini
wal_level = replica
archive_mode = on
archive_command = 'aws s3 cp %p s3://ai-employee-pg-wal/%f'
archive_timeout = 900      # 15 min
```

Replay with `pg_basebackup` + `restore_command` on a fresh standby or
recovery point. See Postgres docs §26.3 (continuous archiving).

## 3. MinIO — `mc mirror` to a second bucket

```bash
# Daily mirror of every primary bucket to the offsite secondary.
mc alias set primary   "$PRIMARY_MINIO_URL"   "$PRIMARY_ACCESS_KEY"   "$PRIMARY_SECRET_KEY"
mc alias set secondary "$SECONDARY_MINIO_URL" "$SECONDARY_ACCESS_KEY" "$SECONDARY_SECRET_KEY"

TS=$(date -u +%Y%m%d)
for bucket in knowledge approval-supplements rca-reports; do
    mc mirror --remove --overwrite \
        "primary/${bucket}" \
        "secondary/${bucket}-${TS}/"
done
```

Restore (single file):

```bash
mc cp "secondary/knowledge-20260622/path/to/file.pdf" \
      "primary/knowledge/path/to/file.pdf"
```

The `--remove` flag prunes objects that no longer exist on the primary
(true mirror semantics); omit it if you prefer snapshot history.

## 4. Redis — hourly `BGSAVE` + AOF

```bash
# Trigger a background save (non-blocking).
redis-cli -u "$REDIS_URL" BGSAVE

# Snapshot files land in $REDIS_DIR/dump.rdb.
# Copy the latest dump to the backup volume:
TS=$(date -u +%Y%m%dT%H%M%SZ)
cp "$REDIS_DIR/dump.rdb" "/var/backups/ai-employee/redis/dump-${TS}.rdb"

# Prune > 3 days (Redis dumps are large and ephemeral by design).
find /var/backups/ai-employee/redis -name 'dump-*.rdb' -mtime +3 -delete
```

Restore:

```bash
# Stop Redis, replace the RDB, restart.
redis-cli -u "$REDIS_URL" SHUTDOWN NOSAVE
cp /var/backups/ai-employee/redis/dump-20260622T020000Z.rdb \
   "$REDIS_DIR/dump.rdb"
systemctl start redis
```

For AOF users, also keep `appendonly.aof`; on a clean restart Redis
replays it after loading the RDB.

## 5. One-shot `scripts/backup.sh`

`scripts/backup.sh` is the operator entry-point — it runs all three steps
in sequence and prints a single summary. See the script header for
required env vars and exit codes.

```bash
export DATABASE_URL=postgres://...
export PRIMARY_MINIO_URL=https://minio-primary.local:9000
export SECONDARY_MINIO_URL=https://minio-secondary.local:9000
export PRIMARY_ACCESS_KEY=... PRIMARY_SECRET_KEY=...
export SECONDARY_ACCESS_KEY=... SECONDARY_SECRET_KEY=...
export REDIS_URL=redis://redis-primary.local:6379
./scripts/backup.sh           # default: all three subsystems
./scripts/backup.sh pg minio  # subset by name
```

The script:

1. Creates a timestamped archive dir under `/var/backups/ai-employee/`.
2. Runs `pg_dump`, `mc mirror`, `redis-cli BGSAVE` in that order.
3. Writes a `MANIFEST.json` next to the archive with sizes, durations,
   checksums, and exit codes.
4. Exits non-zero on the first failed step (the remaining steps are
   skipped, but the partial archive is preserved for triage).

## 6. Kubernetes — `infra/helm/templates/backup-cronjob.yaml`

A `CronJob` runs `scripts/backup.sh` at 02:00 UTC every day. It mounts
the cluster-local service URLs and writes artefacts to a PVC named
`ai-employee-backups` (created by the chart). Offsite shipping is the
SRE's responsibility; the manifest only handles the in-cluster run.

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: ai-employee-backup
spec:
  schedule: "0 2 * * *"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 7
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 1
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: backup
              image: ai-employee/backup:latest
              command: ["/usr/local/bin/backup.sh"]
              envFrom:
                - secretRef: { name: ai-employee-backup-env }
              volumeMounts:
                - name: backup-pvc
                  mountPath: /var/backups/ai-employee
          volumes:
            - name: backup-pvc
              persistentVolumeClaim: { claimName: ai-employee-backups }
```

## 7. Verification (TDD pin)

`tests/test_backup_runbook.py` pins the *contract* the runbook documents
— not the actual backup mechanics (those need live infra). It asserts:

- `scripts/backup.sh` exists, is executable, and `set -euo pipefail`.
- The Helm `CronJob` exists and runs on a daily schedule.
- The Postgres backup step uses `--format=custom` (so `pg_restore` works).
- The MinIO step uses `mc mirror` and writes to a *different* alias.
- The Redis step uses `BGSAVE` (non-blocking) and prunes dumps > 3 days.

If you change a backup primitive, update both the script and the test
in the same TDD commit.

## 8. Incident playbook — restore from backup

1. **Confirm scope.** Check `/var/backups/ai-employee/latest/MANIFEST.json`
   for the most recent successful run; review Slack alerts for the time
   of corruption.
2. **Page on-call DBA** (Postgres) / SRE (MinIO + Redis).
3. **Stop the writers.** Scale the affected service to 0 replicas
   (`kubectl scale deploy/<svc> --replicas=0`) so the restore is not
   clobbered.
4. **Restore per §2–4** above.
5. **Smoke test.** Run `python scripts/m1_smoke.py --json` (RAG path)
   and `tests/rca-replay/` (RCA path) against the restored DB.
6. **Bring services back** to 1 replica, watch `agent_run_success_rate`
   and `approval_wait_time_p95_s` on the dashboard for 30 min.
7. **Postmortem.** Open a follow-up issue within 24 h; cite the
   `MANIFEST.json` and the restore log in the timeline.
