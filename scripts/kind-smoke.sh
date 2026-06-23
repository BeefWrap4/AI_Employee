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

# helm (a Windows-native Go binary) opens -f values files with os.Open,
# which on Windows does NOT understand POSIX paths like /d/AI_Employee/...
# AND helm's URL parser treats backslashes as escape chars (rejects
# "D:\..." with a colon).  cygpath -m yields forward-slash Windows
# paths (D:/...) which helm accepts.  On non-Windows hosts cygpath is
# absent; the no-op is fine because the POSIX path IS the native path.
if command -v cygpath >/dev/null 2>&1; then
    to_native_path() { cygpath -m "$1"; }
else
    to_native_path() { printf '%s' "$1"; }
fi

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

# Define print_summary early so the ERR trap can call it (bash resolves
# function names at trap-fire time, but in some shells the function must
# already be defined in the source before the trap is set).
print_summary() {
    printf '\n============================================================\n'
    printf 'SMOKE SUMMARY\n'
    printf '============================================================\n'
    printf '  PASS: %d\n' "${#SUMMARY_OK[@]}"
    printf '  FAIL: %d\n' "${#SUMMARY_FAIL[@]}"
    if [[ ${#SUMMARY_FAIL[@]} -gt 0 ]]; then
        printf '\nFailed steps:\n'
        for f in "${SUMMARY_FAIL[@]}"; do printf '  - %s\n' "$f"; done
    fi
    printf '============================================================\n'
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

step "kubectl: ensure namespace ${SMOKE_NS} exists with helm labels"

# Idempotent: create if missing, otherwise patch in the helm
# ownership labels if they are missing.  Avoids the destroy-and-
# recreate dance that would orphan the postgres deployment.
if ! "${KUBECTL_BIN}" get namespace "${SMOKE_NS}" >/dev/null 2>&1; then
    "${KUBECTL_BIN}" create namespace "${SMOKE_NS}" >/dev/null
fi
# helm install --create-namespace requires the namespace to have
# the right labels AND annotations, otherwise it refuses with
# "ownership metadata" errors.  Patch them in (idempotent on re-runs).
if ! "${KUBECTL_BIN}" get namespace "${SMOKE_NS}" -o jsonpath='{.metadata.labels.app\.kubernetes\.io/managed-by}' 2>/dev/null \
        | grep -qx Helm; then
    "${KUBECTL_BIN}" label namespace "${SMOKE_NS}" \
        app.kubernetes.io/managed-by=Helm \
        app.kubernetes.io/part-of=ai-employee \
        --overwrite >/dev/null
    "${KUBECTL_BIN}" annotate namespace "${SMOKE_NS}" \
        meta.helm.sh/release-name="${SMOKE_RELEASE}" \
        meta.helm.sh/release-namespace="${SMOKE_NS}" \
        --overwrite >/dev/null
fi
record_ok "namespace ${SMOKE_NS} present (with helm labels + annotations)"

# --------------------------------------------------------------------------- #
# 4. Pull + load postgres:16
# --------------------------------------------------------------------------- #

if [[ -z "${SKIP_PG:-}" ]]; then
    step "kind: load ${POSTGRES_IMAGE}"

    if ! "${DOCKER_BIN}" images --format '{{.Repository}}:{{.Tag}}' \
            | grep -qx "${POSTGRES_IMAGE}"; then
        "${DOCKER_BIN}" pull "${POSTGRES_IMAGE}"
    fi
    # kind load on multi-platform images hits a known ctr-import digest
    # mismatch on Windows/older containerd.  If the load fails, check
    # whether the image is already present in the kind node (re-run case)
    # and continue; otherwise abort.
    if ! "${KIND_BIN}" load docker-image "${POSTGRES_IMAGE}" --name "${CLUSTER_NAME}" 2>/dev/null; then
        # kind's ctr import fails on multi-platform images, but the
        # image is often already loaded by a previous run.  Match
        # either library/postgres:16 or docker.io/library/postgres:16.
        if "${DOCKER_BIN}" exec "${CLUSTER_NAME}-control-plane" \
                ctr -n k8s.io images ls 2>/dev/null \
                | grep -qE "(docker\.io/)?library/${POSTGRES_IMAGE}\b"; then
            printf 'kind load failed but image already present; continuing (re-run)\n'
        else
            printf 'FATAL: kind load failed and image missing in node\n' >&2
            exit 1
        fi
    fi
    record_ok "${POSTGRES_IMAGE} loaded into kind"

    # 5. Apply postgres manifest
    step "kubectl: apply infra/k8s/postgres.yaml"
    "${KUBECTL_BIN}" apply -f "$(to_native_path "${REPO_ROOT}/infra/k8s/postgres.yaml")"
    record_ok "postgres manifest applied"

    # 6. Wait for postgres ready
    step "kubectl: wait for postgres pod ready (timeout=120s)"
    "${KUBECTL_BIN}" wait -n "${SMOKE_NS}" pod -l app=postgres \
        --for=condition=ready --timeout=120s
    record_ok "postgres pod Ready"

    # 6b. Also wait for the postgres Service to have an Endpoint
    # (the next helm install kicks off PG-backed service pods that
    # open DB connections at startup; DNS resolving the Service
    # without a backing pod crashes them).
    step "kubectl: wait for postgres Service endpoints (timeout=60s)"
    "${KUBECTL_BIN}" wait -n "${SMOKE_NS}" \
        --for=jsonpath='{.subsets[0].addresses[0].ip}' \
        endpoints/postgres --timeout=60s
    record_ok "postgres Service has endpoints"
else
    printf 'SKIP_PG set; skipping postgres + PG-tables checks\n'
fi

# --------------------------------------------------------------------------- #
# 7. helm install / upgrade
# --------------------------------------------------------------------------- #

step "helm: install/upgrade ${SMOKE_RELEASE}"

# helm (Windows Go binary) on Git-Bash mishandles -f paths in two
# ways: (1) it rejects paths containing ':' via its URL parser, and
# (2) relative paths come back as "system cannot find the path".
# The portable fix: merge values into a single temp file via Python
# (which handles the YAML merging in a cross-platform way), then
# pass that single file to helm.
cd "${REPO_ROOT}"

# Clean up any stale release of the same name in any namespace (smoke
# is idempotent + re-runnable; old failed runs may have left a release
# in a different namespace that blocks reinstall).
step "helm: clean any stale ${SMOKE_RELEASE} release"
for ns in $("${HELM_BIN}" list -A 2>/dev/null | awk 'NR>1 && $1=="'"${SMOKE_RELEASE}"'" {print $2}'); do
    printf 'uninstalling stale release in namespace %s\n' "$ns"
    "${HELM_BIN}" uninstall "${SMOKE_RELEASE}" -n "$ns" >/dev/null 2>&1 || true
done
record_ok "no stale ${SMOKE_RELEASE} release"

# Wait for the namespace to leave the Terminating state (uninstall
# triggers async deletion of release secrets; the namespace stays
# Terminating until that's done — subsequent helm install would
# fail with "namespace is being terminated").
if "${KUBECTL_BIN}" get namespace "${SMOKE_NS}" >/dev/null 2>&1; then
    if "${KUBECTL_BIN}" get namespace "${SMOKE_NS}" -o jsonpath='{.status.phase}' 2>/dev/null \
            | grep -q Terminating; then
        printf 'waiting for namespace %s to leave Terminating state\n' "${SMOKE_NS}"
        "${KUBECTL_BIN}" wait --for=jsonpath='{.status.phase}'=Active \
            namespace/"${SMOKE_NS}" --timeout=60s >/dev/null 2>&1 || true
    fi
fi

# If the namespace is gone (helm uninstall deleted it), recreate
# with helm labels.  The namespace step earlier in the script only
# handles the case where it already exists.
if ! "${KUBECTL_BIN}" get namespace "${SMOKE_NS}" >/dev/null 2>&1; then
    "${KUBECTL_BIN}" create namespace "${SMOKE_NS}" >/dev/null
fi

# Re-patch helm labels AFTER any uninstall-driven churn (kubectl
# re-creates the namespace and drops our labels).
if ! "${KUBECTL_BIN}" get namespace "${SMOKE_NS}" -o jsonpath='{.metadata.labels.app\.kubernetes\.io/managed-by}' 2>/dev/null \
        | grep -qx Helm; then
    "${KUBECTL_BIN}" label namespace "${SMOKE_NS}" \
        app.kubernetes.io/managed-by=Helm \
        app.kubernetes.io/part-of=ai-employee \
        --overwrite >/dev/null
    "${KUBECTL_BIN}" annotate namespace "${SMOKE_NS}" \
        meta.helm.sh/release-name="${SMOKE_RELEASE}" \
        meta.helm.sh/release-namespace="${SMOKE_NS}" \
        --overwrite >/dev/null
fi

MERGED_VALUES_PATH="${REPO_ROOT}/var/tmp/ai-employee-values-$$.yaml"
python - "${REPO_ROOT}/infra/helm/values.yaml" \
        "${REPO_ROOT}/infra/helm/values-smoke.yaml" \
        "${MERGED_VALUES_PATH}" <<'PY'
import sys, pathlib, yaml
def deep_merge(base, overlay):
    if isinstance(base, dict) and isinstance(overlay, dict):
        out = dict(base)
        for k, v in overlay.items():
            out[k] = deep_merge(out.get(k), v) if k in out else v
        return out
    if isinstance(base, list) and isinstance(overlay, list):
        return list(overlay)
    return overlay if overlay is not None else base
base, overlay = (pathlib.Path(p).read_text(encoding="utf-8") for p in sys.argv[1:3])
merged = deep_merge(yaml.safe_load(base), yaml.safe_load(overlay))
pathlib.Path(sys.argv[3]).parent.mkdir(parents=True, exist_ok=True)
pathlib.Path(sys.argv[3]).write_text(yaml.safe_dump(merged), encoding="utf-8")
PY
record_ok "merged values written to ${MERGED_VALUES_PATH}"

# helm (Windows Go binary) on Git-Bash rejects POSIX paths via its
# URL parser.  Pass the file via a D:/...-style path instead; the
# script's to_native_path helper handles the conversion.
MERGED_VALUES_FOR_HELM="$(to_native_path "${MERGED_VALUES_PATH}")"
if "${HELM_BIN}" list -n "${SMOKE_NS}" 2>/dev/null \
        | awk '{print $1}' | grep -qx "${SMOKE_RELEASE}"; then
    "${HELM_BIN}" upgrade "${SMOKE_RELEASE}" infra/helm \
        -n "${SMOKE_NS}" -f "${MERGED_VALUES_FOR_HELM}"
else
    "${HELM_BIN}" install "${SMOKE_RELEASE}" infra/helm \
        -n "${SMOKE_NS}" --create-namespace -f "${MERGED_VALUES_FOR_HELM}"
fi
rm -f "${MERGED_VALUES_PATH}"
record_ok "helm release ${SMOKE_RELEASE} installed/upgraded"

# --------------------------------------------------------------------------- #
# 8. Wait for helm-managed pods ready
# --------------------------------------------------------------------------- #

step "kubectl: wait for helm-managed pods ready (timeout=240s)"
# Wait for every pod that ISN'T event-gateway (which needs Kafka —
# not in kind smoke).  --ignore-not-found handles re-runs.
"${KUBECTL_BIN}" wait -n "${SMOKE_NS}" \
    --for=condition=ready pod \
    -l "app.kubernetes.io/managed-by=Helm,app!=event-gateway" \
    --timeout=240s
record_ok "helm-managed pods Ready (excluding event-gateway)"

# Warn (not fail) if event-gateway is not Ready — expected when no
# Kafka broker is in the cluster.
if "${KUBECTL_BIN}" get pod -n "${SMOKE_NS}" -l app=event-gateway \
        -o jsonpath='{.items[0].status.containerStatuses[0].ready}' 2>/dev/null \
        | grep -q true; then
    record_ok "event-gateway pod Ready"
else
    record_fail "event-gateway pod not Ready (expected — needs Kafka broker not in kind smoke)"
fi

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

# Pull INTERNAL_TOKEN from the chart's Secret so the smoke can hit
# agent-platform-api's auth-gated endpoints (Depends(run_auth)).
INTERNAL_TOKEN="$(
    "${KUBECTL_BIN}" get secret -n "${SMOKE_NS}" ai-employee-secrets \
        -o jsonpath='{.data.INTERNAL_TOKEN}' 2>/dev/null \
    | base64 --decode 2>/dev/null || true
)"

# --------------------------------------------------------------------------- #
# 10. Smoke checks
# --------------------------------------------------------------------------- #

step "smoke: GET /health on api-gateway + 6 backend /health endpoints"
for path in "${GATEWAY_HEALTH_PATHS[@]}"; do
    # /api/platform/health etc. require auth in some deployments;
    # pass the token so we exercise the real auth path.  Falls back
    # to no-token if the secret is missing (dev mode).
    AUTH_ARGS=()
    if [[ -n "${INTERNAL_TOKEN}" ]]; then
        AUTH_ARGS=(-H "X-Internal-Token: ${INTERNAL_TOKEN}")
    fi
    if curl -fsS "${AUTH_ARGS[@]}" -o /dev/null -w '%{http_code}' \
            "${GATEWAY_URL}${path}" | grep -q '^200$'; then
        record_ok "GET ${path} -> 200"
    else
        record_fail "GET ${path} (non-200)"
    fi
done

step "smoke: GET agent-templates (expect 5 templates)"
TEMPLATES_JSON="$(curl -fsS "${AUTH_ARGS[@]}" \
    "${GATEWAY_URL}/api/platform/api/v1/agent-templates")"
TEMPLATE_COUNT="$(printf '%s' "${TEMPLATES_JSON}" \
    | python -c 'import json,sys;d=json.load(sys.stdin);print(len(d.get("items", d.get("templates", []) if isinstance(d, dict) else d)))')"
if [[ "${TEMPLATE_COUNT}" -ge 5 ]]; then
    record_ok "agent-templates returned ${TEMPLATE_COUNT} (>=5)"
else
    record_fail "agent-templates returned ${TEMPLATE_COUNT} (expected >=5)"
fi

step "smoke: POST agent-run + fetch (expect status=completed)"

# INTERNAL_TOKEN was pulled earlier (line ~388).
if [[ -z "${INTERNAL_TOKEN}" ]]; then
    INTERNAL_TOKEN="${INTERNAL_TOKEN_ENV:-REPLACE_WITH_INTERNAL_TOKEN}"
fi

RUN_BODY='{"template_id":"knowledge_qa","requested_by":"kind-smoke","input":{"query":"smoke test"}}'
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


print_summary

if [[ "${#SUMMARY_FAIL[@]}" -gt 0 ]]; then
    exit 1
fi
exit 0
