# scripts

Automation scripts for local setup, sample data loading, evaluation runs, and maintenance tasks.

## Local Docker

- `powershell -ExecutionPolicy Bypass -File scripts/docker-smoke.ps1 -Json`
  starts the Docker Compose stack, waits for core services, checks HTTP health,
  uploads/publishes a RAG document, runs a cited question, and creates an RCA run.
- `powershell -ExecutionPolicy Bypass -File scripts/docker-smoke.ps1 -NoStart -Json`
  runs the same checks against an already-running stack.
- `powershell -ExecutionPolicy Bypass -File scripts/seed-demo.ps1 -Json`
  seeds demo RAG, RCA, platform agent-run, and tool-registry records for the web portal.
