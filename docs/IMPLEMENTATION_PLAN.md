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
- [x] Phase 8 — tests, lint, coverage: 170 unit/integration tests + 5 opt-in
      live-Docker e2e tests, 96% coverage on `src/`, ruff clean. Found and
      fixed a real bug during this phase: `DockerCollector.__init__`'s
      operator precedence silently ignored an injected `client` whenever
      `socket_url` was unset, so dependency-injected tests were hitting the
      live daemon instead of the mock -- fixed and re-verified live.
- [x] Phase 9 — docs (README, ARCHITECTURE, SETUP, CONFIGURATION, SECURITY,
      REMEDIATION_POLICIES, API, TROUBLESHOOTING, FAILURE_SCENARIOS,
      INTERVIEW_GUIDE), LICENSE, CONTRIBUTING, `scripts/demo.sh`, real
      captured sample outputs in `examples/`, final full-suite validation
- [x] Phase 10 (post-delivery) — pushed to a public GitHub repo (the demo
      GIF/cast are hosted as a GitHub Release asset, not a git blob, after
      repeated `HTTP 408`/"unexpected disconnect" failures on normal pushes
      from two different machines -- see the Troubleshooting doc and the
      release at `releases/tag/demo-v1`); closed the CLI test-coverage gap
      with a 38-test `tests/integration/test_cli.py` suite covering exit
      codes and output for every command via Typer's `CliRunner`. Found and
      fixed two more real bugs along the way:
      1. `print_json()` was piping JSON through Rich's word-wrapping
         console, which could inject literal newlines into long string
         values and corrupt the output as JSON -- fixed by printing
         machine-readable output with plain `print()`, never through Rich.
      2. `scheduler.py` imports `DockerCollector` via `from ... import
         DockerCollector` at module load time, so patching
         `collector.DockerCollector` for a test doesn't reach code that
         goes through `build_scheduler()` -- the first version of the
         `start` command's tests silently fell through to the real Docker
         daemon (22s per test) instead of the fake; fixed by patching both
         bindings. 209 tests passing, 97% coverage on `src/` with `cli/`
         and `api/` no longer excluded from the number.
- [x] Phase 11 (post-delivery) — closed the remaining honest gap: added 3
      live (non-mocked) remediation e2e tests exercising real
      `dry_run=false` restarts against real Docker:
      `TestLiveRestartIsActuallyIssuedAndRecoveryVerified` (a genuine
      restart that recovers, confirmed by polling the real container's
      state/health, then resolves through the normal evaluator path on the
      next cycle -- never marked resolved by the remediation engine itself),
      `TestLiveRemediationFailureWhenHealthNeverRecovers` (a restart whose
      container's healthcheck can never pass, correctly landing on
      `REMEDIATION_FAILED`, never silently reported as fixed), and
      `TestRestartLoopIsDeniedEvenWhenFullyArmed` (a crash-looping
      container with remediation fully enabled and explicitly allowlisted
      -- still never restarted by Guardian, proving the rule-level denial
      holds independent of the global switch). All 3 run against the real
      Colima daemon and clean up their own disposable containers.
      Building the first of these surfaced one more real bug: the
      evaluator's `unexpected_stop` rule had no clearing condition at
      all -- once triggered, that incident could never auto-resolve, so
      every later occurrence on that container would keep absorbing into
      the same ancient incident instead of a fresh one ever being raised.
      Fixed by clearing it once the container is observed running again;
      covered by two new unit tests. tests/e2e is now 8 tests (up from 5).
