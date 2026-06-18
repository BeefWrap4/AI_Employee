# Kubernetes deployment manifests

Base manifests for each AI Employee backend service. Each service runs
as a `Deployment` + `ClusterIP` `Service` + a `ConfigMap` for non-secret
env vars. SQLite persistence (MVP) uses a `PersistentVolumeClaim` per
stateful service; secrets (JWT, API keys) are mounted from a `Secret`.

The Helm chart in `../helm/` wraps these with templated values for
dev/staging/prod environments.

## Services

| Service | Container port | PVC | Notes |
|---------|---------------|-----|-------|
| knowledge-api | 8010 | yes | SQLite + raw uploads |
| ingestion-worker | 8011 | no | stateless worker |
| rca-agent | 8020 | yes | SQLite RCA store |
| agent-platform-api | 8030 | yes | SQLite runs + eval |
| tool-registry | 8040 | yes | SQLite tool store |
| eval-service | 8050 | no | CLI/runner, invoked by platform |

## Apply

```bash
# Create the secret first (JWT signing key, Qwen API key, internal token).
kubectl apply -f secret.yaml

# Apply base manifests.
kubectl apply -f namespace.yaml
kubectl apply -f knowledge-api.yaml
kubectl apply -f ingestion-worker.yaml
kubectl apply -f rca-agent.yaml
kubectl apply -f agent-platform-api.yaml
kubectl apply -f tool-registry.yaml
```
