# Docker Health Guardian

A local-first, free-and-open-source service that watches your Docker
containers, turns raw state/health/resource signals into deduplicated
incidents with an explainable diagnosis, and -- only when you explicitly
opt in -- can safely restart an eligible container and verify it actually
recovered.

Monitoring-only by default. Dry-run by default. Nothing is ever restarted
without an explicit allowlist. Every decision, denial, and attempt is
logged and queryable.

## Problem statement

"Is my container okay?" sounds simple until you need to answer it
reliably: Docker's own health check is binary and has no memory of history,
`docker stats` doesn't tell you what's *normal* for this container, and
blindly auto-restarting anything that looks unhealthy risks restart-looping
a container that's failing for a reason a restart can't fix. This project
exists to sit in between "I noticed something's wrong" and "I did something
about it," with an explicit, auditable decision at every step.

## Features

- **Collector** (`collector/`): wraps the Docker SDK, computes CPU/memory
  from raw stats using Docker's own documented formula (not guessed), and
  never treats "no health check configured" as "unhealthy."
- **Detection** (`detection/`): 10 rules across container-health,
  resource-pressure, and monitoring-reliability categories, each requiring
  a *sustained* breach (rolling windows), not a single sample.
- **Diagnosis** (`diagnosis/`): deterministic, evidence-based -- one
  documented mapping per rule, confidence capped at "medium" for
  threshold-based rules, explicit alternative explanations.
- **Incidents** (`incidents/`): a real lifecycle
  (`DETECTED -> INVESTIGATING -> ... -> RESOLVED`), deduplicated, with a
  cooldown to prevent flapping re-raises.
- **Remediation** (`remediation/`): monitoring-only and dry-run by default;
  explicit allowlist; cooldowns and per-incident action caps; concurrency
  lock; recovery is *verified*, never assumed.
- **Notifications** (`notifications/`): console/email/webhook with
  severity filtering, per-channel cooldown, and bounded retry -- fully unit
  tested with fake SMTP/HTTP clients, no real credentials needed.
- **Persistence** (`persistence/`): SQLite with a versioned schema,
  WAL mode, and retention cleanup that only ever touches old
  resolved/suppressed incidents.
- **API** (`api/`): read-only FastAPI app, localhost by default.
- **CLI** (`cli/`): a full Typer CLI (`guardian ...`, see below).
- **Metrics** (`metrics/`): Prometheus-compatible `/metrics`, bounded label
  values only.
- **Reporting** (`reporting/`): JSON/CSV/HTML, keeping observed facts,
  diagnosis, and verified outcomes in distinct fields.

## Architecture

See `docs/ARCHITECTURE.md` for the full diagrams (component flow, incident
lifecycle, remediation decision tree). In short:

```
Collector -> Evaluator -> IncidentManager -> Diagnosis
                               |                 |
                               v                 v
                        RemediationEngine <-- (policy)
                               |
                               v
                        RecoveryVerifier
                               |
              +----------------+----------------+
              v                v                v
        Persistence    NotificationManager   Metrics
              |
       CLI / API / Reporting
```

## Technology stack

Python 3.11+, Docker SDK for Python, FastAPI + uvicorn, Typer, Pydantic,
SQLite (stdlib `sqlite3`), prometheus-client, Rich (CLI tables), httpx,
pytest + ruff. Optional: Docker Compose, Prometheus, Grafana. Nothing here
requires a paid account or subscription.

## How the monitoring loop works

`Scheduler.run_once()` runs one full cycle, never overlapping with the
next: ping the daemon -> collect a snapshot per container -> evaluate
rules (sustained-window state is kept per container) -> for each newly
triggered rule, dedupe/create the incident, diagnose it, notify, and check
remediation eligibility -> for each cleared rule, resolve the matching open
incident. `guardian start` runs this loop plus the read-only API together;
Ctrl+C (or SIGTERM) stops it gracefully after the current cycle.

## Incident lifecycle

`DETECTED -> INVESTIGATING -> ACTION_APPROVED -> REMEDIATING -> VERIFYING
-> RESOLVED`, with `ACKNOWLEDGED`/`SUPPRESSED`/`ESCALATED`/
`REMEDIATION_FAILED`/`RECOVERY_UNVERIFIED` as alternative/exceptional
states. Most incidents in the shipped monitoring-only config go straight
from `INVESTIGATING` to `RESOLVED` -- see `docs/ARCHITECTURE.md` for the
full state diagram and why `RemediationEngine` never sets `RESOLVED`
itself.

## Diagnosis and remediation logic

Diagnosis is a pure, deterministic function (`diagnosis/engine.py`): one
rule, one evidence-based likely cause, one confidence label, explicit
alternatives, and a `safe_to_automate` flag. Remediation
(`remediation/engine.py`) only ever acts on an *already-diagnosed* incident
and only after passing every check in `docs/REMEDIATION_POLICIES.md`'s
table -- monitoring-only by default, dry-run by default, explicit
allowlist (empty = nothing eligible), protected containers always win,
cooldowns, per-incident action caps, a concurrency lock, and mandatory
post-restart verification before anything is called "recovered."

## Security model

Full detail in `docs/SECURITY.md`. The short version: Docker socket access
is effectively host root, regardless of in-container user -- this project
never pretends otherwise. Within that access, the code only ever calls
list/inspect/stats/logs/restart/events, never create/remove/exec/prune.
The API is read-only and binds to localhost by default.

## Installation

### macOS / Linux

```bash
git clone <this-repo>
cd docker-health-guardian
make install              # python3 -m venv .venv && pip install -e ".[dev]"
. .venv/bin/activate
guardian self-check
```

### Docker prerequisites

A reachable Docker Engine (Docker Desktop, Colima, or a native Linux
daemon -- this project was built and verified against Colima on macOS/
Apple Silicon). `docker-compose` (the standalone binary) if you want the
Compose deployment; the `docker compose` plugin is not required.

### Local development setup

See `docs/SETUP.md` for the full walkthrough, including running via Docker
Compose with the bundled sample workloads, and the optional Prometheus/
Grafana stack.

## Configuration reference

See `docs/CONFIGURATION.md` for every setting, its default, and its
validation rules. Example (`config/guardian.yaml`):

```yaml
guardian:
  polling_interval_seconds: 15
  mode: monitoring
remediation:
  enabled: false
  dry_run: true
  restart_allowlist: []
docker:
  monitored_containers: []
notifications:
  console:
    enabled: true
```

## CLI reference

```
$ guardian --help
Commands:
  containers        List containers Guardian can see, with state and resource usage.
  config-validate   Validate the configuration file and exit.
  start             Run the monitoring loop (and the read-only API) in the foreground.
  status            One-shot snapshot: open incidents, remediation mode, container counts.
  self-check        Run startup diagnostics: config, Docker daemon, persistence.
  incident          Show full detail for one incident, including its event timeline.
  report            Generate an incident/remediation report (JSON, CSV, or an HTML summary).
  cleanup           Delete RESOLVED/SUPPRESSED incidents older than retention_days.
  rules             rules list | rules validate
  incidents         incidents list [--state] [--severity] [--limit] [--json]
  remediation       remediation history | dry-run <container> | enable <container> | disable <container>
```

Every list/show command supports `--json` for scripting. Captured live
during development (see `docs/IMPLEMENTATION_PLAN.md` for the full session
notes):

```
$ guardian rules list
                                Detection Rules
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ Rule ID         ┃ Category        ┃ Severity ┃ Remediation OK ┃ Cooldown (s) ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━┩
│ health_unhealthy│ container_health│ high     │ True           │ 180          │
│ restart_loop    │ container_health│ critical │ False          │ 600          │
│ cpu_sustained_…  │ resource_press… │ medium   │ False          │ 300          │
└─────────────────┴─────────────────┴──────────┴────────────────┴──────────────┘

$ guardian remediation dry-run guardian-demo-unhealthy
guardian-demo-unhealthy: NOT ELIGIBLE
reason: remediation is disabled (monitoring-only mode)
```

## API reference

See `docs/API.md`. `GET /health`, `/ready`, `/api/v1/containers`,
`/api/v1/incidents` (+pagination/filtering), `/api/v1/incidents/{id}`,
`/api/v1/remediation/history`, `/metrics`.

## Prometheus and Grafana setup

```bash
docker-compose -f deploy/docker-compose.yml --profile observability up -d
```

Provisions a Prometheus scrape config (`deploy/prometheus/prometheus.yml`)
and a Grafana dashboard (`grafana/dashboards/guardian-overview.json`,
auto-loaded via file-based provisioning) showing container health,
open incidents by severity, remediation outcomes, recovery verification
results, and monitoring-cycle duration. Both were built and run end-to-end
during this project's own development -- Prometheus confirmed scraping
real metrics, Grafana confirmed loading the dashboard.

## Example incident report

Captured live from the demo stack during development (see
`docs/IMPLEMENTATION_PLAN.md`):

```json
{
  "incident_id": "5ed06042-...",
  "container_name": "guardian-demo-crashloop",
  "rule_id": "restart_loop",
  "severity": "critical",
  "state": "investigating",
  "diagnosis": {
    "likely_cause": "Container is crash-looping -- it starts, fails, and restarts repeatedly",
    "confidence": "high",
    "evidence": ["3 restarts within 300s"],
    "recommended_action": "Do not restart again automatically -- capture logs and escalate to a human",
    "safe_to_automate": false
  }
}
```

## Failure simulation walkthrough

See `docs/FAILURE_SCENARIOS.md` for all 10 required scenarios, each with
what's simulated, the expected incident/diagnosis, whether remediation is
permitted, and how to verify and clean up -- using only the disposable
`guardian-demo-*` Compose project, never any other container on your host.

## Testing

```bash
make test        # unit + integration, ~1-2s, no real Docker mutation
make test-cov     # same, with coverage report
GUARDIAN_RUN_E2E=1 pytest tests/e2e   # opt-in: real disposable containers
make lint
```

**Actual results from this project's own development** (see
`docs/IMPLEMENTATION_PLAN.md` for the full phase-by-phase log): 170
unit/integration tests passing, 96% coverage on `src/`, ruff clean, plus 5
e2e tests passing against a live Colima Docker daemon.

## Troubleshooting

See `docs/TROUBLESHOOTING.md` -- including the exact `DOCKER_GID` fix
needed for the Compose deployment's non-root container to open the
mounted socket, found and resolved during this project's own development.

## Limitations

- Single-host, single Docker daemon per Guardian instance -- no Swarm or
  Kubernetes awareness (out of scope by design, see
  `docs/IMPLEMENTATION_PLAN.md`).
- No authentication on the API; it is read-only and localhost-by-default
  instead, see `docs/SECURITY.md`.
- Retention cleanup (`guardian cleanup`) is manual/cron-driven, not
  automatic inside `guardian start`.
- Reaction latency is bounded by `polling_interval_seconds` (default 15s)
  -- this is a polling design, not event-driven.
- `restart` is the only remediation action actually wired to mutate a
  container today; `capture_logs`/`stop_automation`/`recheck_status`/
  `wait_and_verify`/`escalate` exist in the config schema as named,
  allowed actions but are not yet independently implemented behaviors
  beyond what the restart flow already does (log capture on diagnosis,
  recheck via the evaluator's next cycle, escalation via notification).

## Cost and licensing

Entirely free and open-source (MIT, see `LICENSE`). No paid APIs, cloud
services, or commercial licenses are required for any feature, including
the optional Prometheus/Grafana stack (self-hosted, free images).

## Future enhancements

Honest list, not aspirational marketing (also see `docs/INTERVIEW_GUIDE.md`
Q&A on this): API authentication for non-localhost deployments; automatic
scheduled retention cleanup; independently implemented
`stop_automation`/`recheck_status`/`wait_and_verify` actions as distinct
remediation steps rather than implicit side effects of the restart flow;
Alertmanager integration alongside the existing webhook channel.
