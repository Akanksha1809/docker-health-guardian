# Implementation Plan — Docker Health Guardian

## 1. Environment (verified 2026-10-26)

| Check | Result |
|---|---|
| OS / Arch | macOS (Darwin 24.6.0), Apple Silicon (arm64) |
| CPU / RAM / Disk | 8 cores / 8 GB RAM / ~16 GB free |
| Python | 3.15.0 at `/opt/homebrew/bin/python3`, pip 26.2.2 |
| Git | 2.50.1, no global identity configured (set locally for this repo) |
| Docker Engine | 29.6.1 (server 29.5.2), via Colima, daemon **reachable** |
| Docker Compose | `docker compose` plugin NOT installed; standalone `docker-compose` 5.3.1 **works** — Compose files target `docker-compose` |
| Docker socket | `/var/run/docker.sock` (symlinked to Colima's socket) — reachable from this shell |
| Pre-existing containers | `devsecops-platform-control-plane` (kind) and `ai-platform-engineer-jenkins` are already running, **unrelated to this project** — Guardian must never touch them; all demo/test containers use the `guardian-demo-` name prefix and/or dedicated Compose project name |

Conclusion: Docker integration, including live container restart tests, **can** be verified end-to-end in this environment. No component needs to be skipped for lack of Docker.

## 2. Architecture

```
Scheduler → Collector → Evaluator → IncidentManager → DiagnosisEngine
                                          │                 │
                                          ▼                 ▼
                                   RemediationEngine ← (policy)
                                          │
                                          ▼
                                  RecoveryVerifier
                                          │
                        ┌─────────────────┼─────────────────┐
                        ▼                 ▼                 ▼
                 PersistenceLayer   NotificationManager   Prometheus metrics
                        │
                 CLI / API / Reporting (read persistence + trigger actions)
```

Package layout under `src/docker_health_guardian/`: `collector/`, `detection/`, `diagnosis/`, `incidents/`, `remediation/`, `notifications/`, `persistence/`, `api/`, `cli/`, `reporting/`, `config/`, `metrics/`, `scheduler.py`.

Data flow is one-directional and synchronous per cycle: the scheduler runs one collection cycle at a time (no overlap), feeds container snapshots to rule evaluation, which raises/updates incidents, which may trigger diagnosis → remediation (if policy allows) → verification, with every step persisted and optionally notified/exported as metrics.

## 3. Phases

1. **Done** — environment inspection, this plan.
2. Package skeleton, Pydantic config + validation, Docker collector (state/stats/health), basic Typer CLI (`containers`, `config validate`, `self-check`).
3. Detection rules (rolling windows), diagnosis engine, incident lifecycle + dedup, SQLite persistence with schema/migration.
4. Remediation engine (allowlist/cooldown/dry-run/monitoring-only) + recovery verifier.
5. Notifications (console/email/webhook with retry), reporting (JSON/CSV/HTML), FastAPI app.
6. Prometheus metrics endpoint + Grafana dashboard + scrape config.
7. Docker Compose deployment + failure-simulation toolkit using disposable `guardian-demo-*` containers.
8. Full test suite (unit/integration/e2e) + Ruff lint + coverage report.
9. Documentation set + final validation + demo script.

Each phase ends with: files touched, tests run, actual pass/fail, checklist update.

## 4. Dependencies (all free/OSS, pinned loosely)

`docker`, `fastapi`, `uvicorn`, `typer`, `pydantic`, `pydantic-settings`, `pyyaml`, `prometheus-client`, `rich` (CLI tables), `httpx` (webhook/test client) — dev: `pytest`, `pytest-cov`, `ruff`, `types-*` as needed.

## 5. Risks / mitigations

- **Docker stats API quirks** (missing `precpu_stats` on first sample, cgroup v1 vs v2 memory fields) → defensive parsing, unit tests with synthetic payloads, treat "unavailable" as its own state rather than 0%.
- **Flaky CPU/memory-based tests** → rule/unit tests use synthetic metrics only; real stress tests are manual/optional (`examples/`), never part of default `pytest` run.
- **Accidental interference with the user's existing containers** → collector supports `monitored_containers`/`excluded_containers`; e2e tests and failure simulations only ever create/touch containers with a dedicated label (`guardian.demo=true`) and name prefix, and refuse to act on anything else.
- **SQLite concurrency** between scheduler thread and API/CLI reads → short-lived connections per operation, WAL mode, explicit transactions.
- **No `docker compose` plugin** → all compose usage documented with `docker-compose` (standalone) command; README notes both.

## 6. Acceptance criteria

See section 24 of the master prompt — tracked as a living checklist in this file, updated at the end of each phase.

### Phase checklist

- [x] Phase 1 — environment inspection, plan
- [x] Phase 2 — skeleton, config, collector, CLI
- [x] Phase 3 — detection, diagnosis, incidents, persistence
- [x] Phase 4 — remediation, recovery verification
- [x] Phase 5 — notifications, reporting, API
- [x] Phase 6 — Prometheus/Grafana
- [x] Phase 7 — Compose deployment, failure simulation (verified live: full
      stack built and run, Prometheus confirmed scraping, Grafana dashboard
      confirmed provisioned, all demo containers cleanly torn down)
- [ ] Phase 8 — tests, lint, coverage
- [ ] Phase 9 — docs, final validation, demo script
