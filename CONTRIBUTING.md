# Contributing

## Setup

```bash
make install   # creates .venv and installs the package + dev dependencies
make test      # runs the unit + integration suite
make lint       # ruff check
```

## Guidelines

- New detection rules go in `src/docker_health_guardian/detection/rules.py`
  (the catalog entry) and `evaluator.py` (the trigger/clear logic), with a
  matching diagnosis in `diagnosis/engine.py` and unit tests using synthetic
  `ContainerSnapshot`s -- never a real Docker daemon for rule logic.
- Anything that touches a real Docker container in a test must use the
  `guardian-e2e-` (or `guardian-demo-`) name prefix, live in `tests/e2e/`,
  and clean up after itself even on failure.
- Keep `monitoring_only`/`dry_run` the default behavior for anything new in
  the remediation path. A new remediation action needs its own eligibility
  checks, an audit record, and a recovery-verification step, mirroring
  `remediation/engine.py`.
- Run `make lint` and `make test` before opening a PR; `make test-cov`
  reports coverage (target: 85%+ on `src/`, meaningful tests only -- no
  padding assertions just to move the number).
- Commit messages and PRs should say *why*, not just *what* -- the code
  already shows what changed.

## Project scope

This is intentionally a single-host, Docker-only tool. PRs that add
Kubernetes, a message broker, or cloud-provider integrations are out of
scope -- see `docs/IMPLEMENTATION_PLAN.md` for the reasoning.
