# Setup

## Prerequisites

- Python 3.11+ (tested on 3.12/3.15)
- Docker Engine, reachable from the shell you'll run Guardian in (Docker
  Desktop, Colima, or a native Linux daemon all work -- verified in this
  project against Colima on macOS/Apple Silicon)
- `docker-compose` (the standalone binary; the `docker compose` plugin is
  **not** required -- this project's Makefile and docs always use
  `docker-compose`)
- Optional: `make` (all commands also work run directly, see below)

## Local development (no containers)

```bash
git clone <this-repo>
cd docker-health-guardian
make install              # creates .venv, installs the package + dev deps
. .venv/bin/activate
guardian self-check       # verifies config, Docker daemon, persistence
guardian containers       # lists what Guardian can currently see
guardian start             # runs the monitoring loop + API in the foreground
```

`guardian self-check` is the fastest way to confirm your environment is
ready; it validates configuration, pings the Docker daemon, and initializes
the SQLite database, each reported independently with a clear `OK`/`FAIL`.

## Running via Docker Compose (service + sample workloads)

This brings up Guardian itself plus six disposable `guardian-demo-*`
containers (see `docs/FAILURE_SCENARIOS.md`) for a self-contained
demonstration that never touches any other containers on your host.

```bash
export DOCKER_GID=$(docker run --rm -v /var/run/docker.sock:/var/run/docker.sock busybox:1.36 stat -c '%g' /var/run/docker.sock)
docker-compose -f deploy/docker-compose.yml up -d --build
curl localhost:8420/health
curl localhost:8420/api/v1/incidents | python3 -m json.tool
docker-compose -f deploy/docker-compose.yml down       # add -v to also drop incident history
```

`DOCKER_GID` matters because the Guardian container runs as a non-root user
that needs permission to open the mounted Docker socket -- see
`docs/SECURITY.md` for why this is detected this way rather than guessed.

### With Prometheus + Grafana

```bash
docker-compose -f deploy/docker-compose.yml --profile observability up -d
open http://localhost:3000   # Grafana, admin/admin, anonymous read enabled
open http://localhost:9090   # Prometheus
```

Both `docker-compose.yml` and the `--profile observability` variant were
built and run end-to-end while developing this project; Prometheus was
confirmed scraping real metrics and the provisioned Grafana dashboard was
confirmed loading from its file-based provider (see
`docs/IMPLEMENTATION_PLAN.md` for the session's verification notes).

## Running tests

```bash
make test                           # unit + integration (fast, no real Docker mutation)
make test-cov                       # same, with a coverage report
GUARDIAN_RUN_E2E=1 pytest tests/e2e  # opt-in: creates and removes real disposable containers
```

## Cleaning up

```bash
docker-compose -f deploy/docker-compose.yml down -v   # stop + remove demo containers and volume
rm -rf data/ .venv/ .pytest_cache/ .ruff_cache/
```
