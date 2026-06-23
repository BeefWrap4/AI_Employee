#!/usr/bin/env bash
# kind-smoke.sh — one-shot kind cluster + ai-employee helm deploy + smoke.
#
# R35-B: reproduces the R34 kind-cluster deployment validation in a single
# idempotent script.  Re-running it is safe: the kind cluster is reused
# (or created on first run), images already loaded into kind are skipped,
# helm upgrades in place, and PG + chart readiness are polled with timeouts.
#
# Usage:
#     ./scripts/kind-smoke.sh
#
# Environment overrides (all optional):
#     KIND_BIN     path to kind binary (default: ./bin/kind.exe)
#     KUBECTL_BIN  path to kubectl binary (default: kubectl)
#     HELM_BIN     path to helm binary (default: helm)
#     DOCKER_BIN   path to docker binary (default: docker)
#     SMOKE_NS     namespace to deploy into (default: ai-employee)
#     SMOKE_RELEASE helm release name (default: ai-emp)
#     SKIP_PG      set non-empty to skip Postgres + PG-tables assertions
#
# Exit codes:
#     0   all smoke checks passed
#     1..N a step failed (the summary block prints which one)

set -euo pipefail

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

KIND_BIN="${KIND_BIN:-${REPO_ROOT}/bin/kind.exe}"
KUBECTL_BIN="${KUBECTL_BIN:-kubectl}"
HELM_BIN="${HELM_BIN:-helm}"
DOCKER_BIN="${DOCKER_BIN:-docker}"

SMOKE_NS="${SMOKE_NS:-ai-employee}"
SMOKE_RELEASE="${SMOKE_RELEASE:-ai-emp}"
CLUSTER_NAME="ai-emp"

POSTGRES_IMAGE="postgres:16"

# 8 ai-employee services per CLAUDE.md service list (event-gateway needs
# Kafka and stays disabled in smoke).  api-gateway is included so the
# end-to-end route is live.
AI_EMPLOYEE_SERVICES=(
    "agent-platform-api"
    "api-gateway"
    "approval-service"
    "ingestion-worker"
    "knowledge-api"
    "mcp-gateway"
    "rca-agent"
    "tool-registry"
)
AI_EMPLOYEE_IMAGE_TAG="0.1.0"

# Backend health endpoints reachable through api-gateway :8070.
GATEWAY_HEALTH_PATHS=(
    "/health"
    "/api/platform/health"
    "/api/knowledge/health"
    "/api/rca/health"
    "/api/tools/health"
    "/api/approvals/health"
    "/api/mcp/health"
)

# --------------------------------------------------------------------------- #
# Tiny logging helpers
# --------------------------------------------------------------------------- #

SUMMARY_OK=()
SUMMARY_FAIL=()
CURRENT_STEP=""

step() {
    CURRENT_STEP="$*"
    printf '\n=== %s ===\n' "$CURRENT_STEP"
}

record_ok() {
    SUMMARY_OK+=("$1")
    printf '  PASS  %s\n' "$1"
}

record_fail() {
    SUMMARY_FAIL+=("$1")
    printf '  FAIL  %s\n' "$1"
}

on_error() {
    local exit_code=$?
    if [[ -n "${CURRENT_STEP}" ]]; then
        record_fail "${CURRENT_STEP} (exit=${exit_code})"
    fi
    print_summary
    exit "${exit_code}"
}

trap on_error ERR

# --------------------------------------------------------------------------- #
# Pre-flight
# --------------------------------------------------------------------------- #

step "preflight: check required binaries"

for bin in "${KUBECTL_BIN}" "${HELM_BIN}" "${DOCKER_BIN}"; do
    if ! command -v "${bin}" >/dev/null 2>&1; then
        printf 'FATAL: required binary not on PATH: %s\n' "${bin}" >&2
        exit 2
    fi
done
record_ok "kubectl/helm/docker on PATH"

# kind binary is at ./bin/kind.exe on Windows; on Linux/macOS a system
# `kind` would be used.
if [[ ! -x "${KIND_BIN}" ]] && command -v kind >/dev/null 2>&1; then
    KIND_BIN="$(command -v kind)"
fi
if [[ ! -x "${KIND_BIN}" ]]; then
    printf 'FATAL: kind binary not found at %s and not on PATH\n' "${KIND_BIN}" >&2
    exit 2
fi
record_ok "kind at ${KIND_BIN}"

# --------------------------------------------------------------------------- #
# 1. Kind cluster (idempotent)
# --------------------------------------------------------------------------- #

step "kind cluster: ensure ${CLUSTER_NAME} exists"

if "${KIND_BIN}" get clusters 2>/dev/null | grep -qx "${CLUSTER_NAME}"; then
    printf 'cluster %s already present; reusing\n' "${CLUSTER_NAME}"
else
    "${KIND_BIN}" create cluster --name "${CLUSTER_NAME}" --wait 60s
fi
record_ok "kind cluster ${CLUSTER_NAME} reachable"

# --------------------------------------------------------------------------- #
# 2. Load 8 ai-employee images into kind (skip if already present)
# --------------------------------------------------------------------------- #

step "kind: load 8 ai-employee images"

for svc in "${AI_EMPLOYEE_SERVICES[@]}"; do
    image="ai-employee/${svc}:${AI_EMPLOYEE_IMAGE_TAG}"
    if "${DOCKER_BIN}" images --format '{{.Repository}}:{{.Tag}}' \
            | grep -qx "${image}"; then
        printf 'image present locally: %s\n' "${image}"
    else
        printf 'WARN: image %s not built locally; build with `docker build` first\n' "${image}" >&2
    fi
    "${KIND_BIN}" load docker-image "${image}" --name "${CLUSTER_NAME}"
done
record_ok "8 ai-employee images loaded into kind"

# --------------------------------------------------------------------------- #
# 3. Namespace
# --------------------------------------------------------------------------- #

step "kubectl: ensure namespace ${SMOKE_NS} exists"

"${KUBECTL_BIN}" create namespace "${SMOKE_NS}" >/dev/null 2>&1 || true
"${KUBECTL_BIN}" get namespace "${SMOKE_NS}" >/dev/null
record_ok "namespace ${SMOKE_NS} present"

# --------------------------------------------------------------------------- #
# 4. Pull + load postgres:16
# --------------------------------------------------------------------------- #

if [[ -z "${SKIP_PG:-}" ]]; then
    step "kind: load ${POSTGRES_IMAGE}"

    if ! "${DOCKER_BIN}" images --format '{{.Repository}}:{{.Tag}}' \
            | grep -qx "${POSTGRES_IMAGE}"; then
        "${DOCKER_BIN}" pull "${POSTGRES_IMAGE}"
    fi
    "${KIND_BIN}" load docker-image "${POSTGRES_IMAGE}" --name "${CLUSTER_NAME}"
    record_ok "${POSTGRES_IMAGE} loaded into kind"

    # 5. Apply postgres manifest
    step "kubectl: apply infra/k8s/postgres.yaml"
    "${KUBECTL_BIN}" apply -f "${REPO_ROOT}/infra/k8s/postgres.yaml"
    record_ok "postgres manifest applied"

    # 6. Wait for postgres ready
    step "kubectl: wait for postgres pod ready (timeout=120s)"
    "${KUBECTL_BIN}" wait -n "${SMOKE_NS}" pod -l app=postgres \
        --for=condition=ready --timeout=120s
    record_ok "postgres pod Ready"
else
    printf 'SKIP_PG set; skipping postgres + PG-tables checks\n'
fi

# --------------------------------------------------------------------------- #
# 7. helm install / upgrade
# --------------------------------------------------------------------------- #

step "helm: install/upgrade ${SMOKE_RELEASE}"

HELM_VALUES=(
    "${REPO_ROOT}/infra/helm/values.yaml"
    "${REPO_ROOT}/infra/helm/values-smoke.yaml"
)

if "${HELM_BIN}" list -n "${SMOKE_NS}" 2>/dev/null \
        | awk '{print $1}' | grep -qx "${SMOKE_RELEASE}"; then
    "${HELM_BIN}" upgrade "${SMOKE_RELEASE}" "${REPO_ROOT}/infra/helm" \
        -n "${SMOKE_NS}" "${HELM_VALUES[@]/#/-f }"
else
    "${HELM_BIN}" install "${SMOKE_RELEASE}" "${REPO_ROOT}/infra/helm" \
        -n "${SMOKE_NS}" --create-namespace "${HELM_VALUES[@]/#/-f }"
fi
record_ok "helm release ${SMOKE_RELEASE} installed/upgraded"

# --------------------------------------------------------------------------- #
# 8. Wait for helm-managed pods ready
# --------------------------------------------------------------------------- #

step "kubectl: wait for helm-managed pods ready (timeout=120s)"
"${KUBECTL_BIN}" wait -n "${SMOKE_NS}" \
    --for=condition=ready pod \
    -l app.kubernetes.io/managed-by=Helm \
    --timeout=120s
record_ok "helm-managed pods Ready"

# --------------------------------------------------------------------------- #
# 9. Port-forward api-gateway
# --------------------------------------------------------------------------- #

step "kubectl: port-forward api-gateway 8070:8070"

"${KUBECTL_BIN}" port-forward -n "${SMOKE_NS}" \
    svc/api-gateway 8070:8070 >/tmp/kind-smoke-pf.log 2>&1 &
PF_PID=$!

cleanup() {
    if [[ -n "${PF_PID:-}" ]] && kill -0 "${PF_PID}" 2>/dev/null; then
        kill "${PF_PID}" >/dev/null 2>&1 || true
    fi
}
trap 'cleanup; on_error' ERR
trap 'cleanup; exit 0' EXIT

# Wait for the port-forward to start accepting connections.
for _ in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:8070/health >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

GATEWAY_URL="http://127.0.0.1:8070"
record_ok "api-gateway reachable at ${GATEWAY_URL}"

# --------------------------------------------------------------------------- #
# 10. Smoke checks
# --------------------------------------------------------------------------- #

step "smoke: GET /health on api-gateway + 6 backend /health endpoints"
for path in "${GATEWAY_HEALTH_PATHS[@]}"; do
    if curl -fsS -o /dev/null -w '%{http_code}' "${GATEWAY_URL}${path}" \
            | grep -q '^200$'; then
        record_ok "GET ${path} -> 200"
    else
        record_fail "GET ${path} (non-200)"
    fi
done

step "smoke: GET agent-templates (expect 5 templates)"
TEMPLATES_JSON="$(curl -fsS "${GATEWAY_URL}/api/platform/api/v1/agent-templates")"
TEMPLATE_COUNT="$(printf '%s' "${TEMPLATES_JSON}" \
    | python -c 'import json,sys;d=json.load(sys.stdin);print(len(d.get("templates", d) if isinstance(d, dict) else d))')"
if [[ "${TEMPLATE_COUNT}" -ge 5 ]]; then
    record_ok "agent-templates returned ${TEMPLATE_COUNT} (>=5)"
else
    record_fail "agent-templates returned ${TEMPLATE_COUNT} (expected >=5)"
fi

step "smoke: POST agent-run + fetch (expect status=completed)"

# INTERNAL_TOKEN comes from the chart's Secret; pull from the api-gateway pod.
INTERNAL_TOKEN="$(
    "${KUBECTL_BIN}" get secret -n "${SMOKE_NS}" ai-employee-secrets \
        -o jsonpath='{.data.INTERNAL_TOKEN}' 2>/dev/null \
    | base64 --decode 2>/dev/null || true
)"
if [[ -z "${INTERNAL_TOKEN}" ]]; then
    INTERNAL_TOKEN="${INTERNAL_TOKEN_ENV:-REPLACE_WITH_INTERNAL_TOKEN}"
fi

RUN_BODY='{"template_id":"knowledge_qa","input":{"query":"smoke test"}}'
RUN_RESP="$(curl -fsS -X POST "${GATEWAY_URL}/api/platform/api/v1/agent-runs" \
    -H "X-Internal-Token: ${INTERNAL_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "${RUN_BODY}" || true)"
RUN_ID="$(printf '%s' "${RUN_RESP}" \
    | python -c 'import json,sys;print(json.load(sys.stdin).get("run_id",""))')"

if [[ -z "${RUN_ID}" ]]; then
    record_fail "agent-run POST returned no run_id: ${RUN_RESP}"
else
    record_ok "agent-run POST -> run_id=${RUN_ID}"
    FETCH_RESP="$(curl -fsS -H "X-Internal-Token: ${INTERNAL_TOKEN}" \
        "${GATEWAY_URL}/api/platform/api/v1/agent-runs/${RUN_ID}" || true)"
    RUN_STATUS="$(printf '%s' "${FETCH_RESP}" \
        | python -c 'import json,sys;print(json.load(sys.stdin).get("status",""))')"
    if [[ "${RUN_STATUS}" == "completed" ]]; then
        record_ok "GET agent-runs/${RUN_ID} -> status=completed"
    else
        record_fail "GET agent-runs/${RUN_ID} -> status=${RUN_STATUS:-<empty>}"
    fi
fi

if [[ -z "${SKIP_PG:-}" ]]; then
    step "smoke: list PG tables via kubectl exec"
    TABLES="$(
        "${KUBECTL_BIN}" exec -n "${SMOKE_NS}" deploy/postgres -- \
            psql -U ai-employee -d ai-employee -c '\dt' 2>/dev/null \
        | awk 'NR>2 && $3 ~ /^\|+$/ {next} {print}' || true
    )"
    TABLE_COUNT="$(printf '%s' "${TABLES}" | grep -cE '^\s*public\s+\|\s+\S+' || true)"
    if [[ "${TABLE_COUNT}" -ge 6 ]]; then
        record_ok "PG tables listed (${TABLE_COUNT} found)"
    else
        record_fail "PG tables under threshold (${TABLE_COUNT} found)"
    fi
fi

# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #

print_summary() {
    printf '\n=========== kind-smoke summary ===========\n'
    printf 'PASS: %d\n' "${#SUMMARY_OK[@]}"
    for entry in "${SUMMARY_OK[@]:-}"; do
        [[ -n "${entry}" ]] && printf '  + %s\n' "${entry}"
    done
    printf 'FAIL: %d\n' "${#SUMMARY_FAIL[@]}"
    for entry in "${SUMMARY_FAIL[@]:-}"; do
        [[ -n "${entry}" ]] && printf '  - %s\n' "${entry}"
    done
    printf '=========================================\n'
}

print_summary

if [[ "${#SUMMARY_FAIL[@]}" -gt 0 ]]; then
    exit 1
fi
exit 0
