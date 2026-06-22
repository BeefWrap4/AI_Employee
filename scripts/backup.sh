#!/usr/bin/env bash
# scripts/backup.sh — one-shot backup of the AI Employee stack.
#
# Runs three steps in order: pg_dump (Postgres) → mc mirror (MinIO) →
# redis BGSAVE (Redis). Each step writes into a timestamped archive dir
# and a MANIFEST.json records sizes / durations / exit codes.
#
# Usage:
#     ./scripts/backup.sh                # all three
#     ./scripts/backup.sh pg minio       # subset
#
# Required env (subset, see runbook §1):
#   DATABASE_URL, PRIMARY_MINIO_URL, SECONDARY_MINIO_URL,
#   PRIMARY_ACCESS_KEY, PRIMARY_SECRET_KEY,
#   SECONDARY_ACCESS_KEY, SECONDARY_SECRET_KEY, REDIS_URL
#
# Exit codes: 0 = success; first failing step's exit code wins.

set -euo pipefail

BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/ai-employee}"
TS=$(date -u +%Y%m%dT%H%M%SZ)
ARCHIVE_DIR="${BACKUP_ROOT}/${TS}"
MANIFEST="${ARCHIVE_DIR}/MANIFEST.json"

mkdir -p "${ARCHIVE_DIR}"
echo "[]" > "${MANIFEST}"

# Append a step result to the manifest. Args: name, status, seconds, size_bytes, detail
record_step() {
    local name="$1" status="$2" seconds="$3" size="$4" detail="$5"
    # Build JSON safely with python (avoids jq dependency).
    python - "$name" "$status" "$seconds" "$size" "$detail" "${MANIFEST}" <<'PY'
import json, sys
name, status, seconds, size, detail, manifest_path = sys.argv[1:]
entry = {
    "step": name,
    "status": status,
    "seconds": float(seconds),
    "size_bytes": int(size),
    "detail": detail,
}
try:
    with open(manifest_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
except (FileNotFoundError, json.JSONDecodeError):
    data = []
data.append(entry)
with open(manifest_path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2)
PY
}

# --------------------------------------------------------------------------- #
# Step 1: PostgreSQL
# --------------------------------------------------------------------------- #

run_pg() {
    local out="${ARCHIVE_DIR}/ai-employee-${TS}.dump"
    local start size seconds status detail
    start=$(date +%s)
    detail="${DATABASE_URL:-<unset>}"
    if [[ -z "${DATABASE_URL:-}" ]]; then
        record_step "pg" "skipped" 0 0 "DATABASE_URL not set"
        return 0
    fi
    if pg_dump "${DATABASE_URL}" \
            --format=custom --compress=9 \
            --no-owner --no-privileges \
            --file="${out}"; then
        size=$(stat -c %s "${out}" 2>/dev/null || echo 0)
        status="ok"
    else
        status="failed"
        size=0
    fi
    seconds=$(( $(date +%s) - start ))
    record_step "pg" "${status}" "${seconds}" "${size}" "${detail}"
    [[ "${status}" == "ok" ]] || return 1
    return 0
}

# --------------------------------------------------------------------------- #
# Step 2: MinIO — mc mirror to a *secondary* alias
# --------------------------------------------------------------------------- #

run_minio() {
    local start seconds status detail
    start=$(date +%s)
    detail="primary→secondary"
    if [[ -z "${PRIMARY_MINIO_URL:-}" || -z "${SECONDARY_MINIO_URL:-}" ]]; then
        record_step "minio" "skipped" 0 0 "MinIO URLs not set"
        return 0
    fi
    mc alias set primary   "${PRIMARY_MINIO_URL}"   "${PRIMARY_ACCESS_KEY:-}"   "${PRIMARY_SECRET_KEY:-}"   >/dev/null
    mc alias set secondary "${SECONDARY_MINIO_URL}" "${SECONDARY_ACCESS_KEY:-}" "${SECONDARY_SECRET_KEY:-}" >/dev/null
    local total=0
    for bucket in knowledge approval-supplements rca-reports; do
        if mc mirror --remove --overwrite "primary/${bucket}" "secondary/${bucket}-${TS}/" 2>/dev/null; then
            : # size is hard to compute; rely on the archive dir for triage
        else
            status="failed"
            seconds=$(( $(date +%s) - start ))
            record_step "minio" "${status}" "${seconds}" "${total}" "${detail}"
            return 1
        fi
    done
    status="ok"
    seconds=$(( $(date +%s) - start ))
    record_step "minio" "${status}" "${seconds}" "${total}" "${detail}"
    return 0
}

# --------------------------------------------------------------------------- #
# Step 3: Redis — hourly BGSAVE
# --------------------------------------------------------------------------- #

run_redis() {
    local start seconds status detail size
    start=$(date +%s)
    detail="${REDIS_URL:-<unset>}"
    if [[ -z "${REDIS_URL:-}" ]]; then
        record_step "redis" "skipped" 0 0 "REDIS_URL not set"
        return 0
    fi
    if redis-cli -u "${REDIS_URL}" BGSAVE >/dev/null; then
        # Wait for BGSAVE to complete; redis-cli exits immediately on success.
        local i=0
        while (( i < 30 )); do
            if [[ "$(redis-cli -u "${REDIS_URL}" LASTSAVE)" != "$(redis-cli -u "${REDIS_URL}" LASTSAVE_BEFORE 2>/dev/null || echo 0)" ]]; then
                break
            fi
            sleep 1
            i=$(( i + 1 ))
        done
        local dump="${REDIS_DIR:-/var/lib/redis}/dump.rdb"
        if [[ -f "${dump}" ]]; then
            cp "${dump}" "${ARCHIVE_DIR}/dump-${TS}.rdb"
            size=$(stat -c %s "${ARCHIVE_DIR}/dump-${TS}.rdb" 2>/dev/null || echo 0)
        else
            size=0
        fi
        status="ok"
    else
        status="failed"
        size=0
    fi
    seconds=$(( $(date +%s) - start ))
    record_step "redis" "${status}" "${seconds}" "${size}" "${detail}"
    [[ "${status}" == "ok" ]] || return 1
    return 0
}

# --------------------------------------------------------------------------- #
# Dispatch — supports subset selection by step name
# --------------------------------------------------------------------------- #

STEPS=("$@")
if [[ ${#STEPS[@]} -eq 0 ]]; then
    STEPS=("pg" "minio" "redis")
fi

for step in "${STEPS[@]}"; do
    case "${step}" in
        pg)     run_pg     ;;
        minio)  run_minio  ;;
        redis)  run_redis  ;;
        *)
            echo "unknown step: ${step}" >&2
            exit 2
            ;;
    esac
done

# Convenience symlink for the on-call to find the latest run.
ln -sfn "${TS}" "${BACKUP_ROOT}/latest"

echo "backup complete: ${ARCHIVE_DIR}"
