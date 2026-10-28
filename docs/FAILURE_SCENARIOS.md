# Failure Simulation & Demonstration

All scenarios use the isolated Compose project in [`deploy/docker-compose.yml`](../deploy/docker-compose.yml)
(project name `guardian-demo`, every container prefixed `guardian-demo-` and
labeled `guardian.demo=true`). **Nothing here ever targets a container
outside that project.** Bring the stack up with:

```bash
export DOCKER_GID=$(docker run --rm -v /var/run/docker.sock:/var/run/docker.sock busybox:1.36 stat -c '%g' /var/run/docker.sock)
docker-compose -f deploy/docker-compose.yml up -d --build
```

and tear it down with `docker-compose -f deploy/docker-compose.yml down`
(add `-v` to also drop the `guardian-data` volume and its incident history).

Guardian itself starts in **monitoring-only mode** (`remediation.enabled:
false` in `config/guardian.yaml`) -- every scenario below is observable
without any container ever being restarted automatically. A later section
shows how to opt into remediation for the restart-eligible scenarios.

---

### 1. Container exits unexpectedly

- **Simulated**: a process inside a container crashes with a nonzero exit code.
- **Container**: `guardian-demo-crashloop` on its very first exit (before the restart-loop rule also fires).
- **Expected incident**: `unexpected_stop`, severity high.
- **Expected diagnosis**: "Process inside the container exited unexpectedly (exit code 1)", confidence high, alternatives include OOM-kill and uncaught exception.
- **Remediation permitted**: yes (rule allows it), but denied by default because `remediation.enabled: false`.
- **Expected notification**: console notification with the diagnosis and evidence.
- **Verify**: `guardian incidents list --state investigating` or `curl localhost:8420/api/v1/incidents`.
- **Cleanup**: `docker-compose -f deploy/docker-compose.yml restart demo-crashloop` or just leave it (it is disposable).

### 2. Container health check fails

- **Simulated**: Docker's own `HEALTHCHECK` reports `unhealthy`.
- **Container**: `guardian-demo-unhealthy` (healthcheck is `exit 1`, always fails).
- **Expected incident**: `health_unhealthy`, severity high, confidence high.
- **Expected diagnosis**: application-level health check failing; recommends inspecting logs before restarting.
- **Remediation permitted**: yes, denied by default (monitoring-only).
- **Expected notification**: yes, immediately (no sustained window for this rule -- Docker already debounces via its own `retries`).
- **Verify**: `docker inspect guardian-demo-unhealthy --format '{{.State.Health.Status}}'` shows `unhealthy`; `guardian incident show <id>`.
- **Cleanup**: none needed; container keeps running.

### 3. Container becomes unresponsive (stuck starting)

- **Simulated**: a container that never leaves Docker's `starting` health state.
- **Container**: temporarily edit `demo-unhealthy`'s healthcheck `start_period` to a large value, or add a new service whose healthcheck command hangs.
- **Expected incident**: `startup_health_timeout`, severity medium.
- **Expected diagnosis**: "never reached a healthy state after starting"; recommends checking `start_period` before restarting.
- **Remediation permitted**: yes, denied by default.
- **Expected notification**: yes, once `startup_health_grace_seconds` elapses.
- **Verify**: `guardian incidents list` shows the rule; event timeline shows seconds-since-start.
- **Cleanup**: revert the healthcheck change.

### 4. CPU exceeds threshold

- **Simulated**: a tight busy-loop pins a CPU core.
- **Container**: `guardian-demo-cpu-stress` (`while true; do :; done`).
- **Expected incident**: `cpu_sustained_high` after `sustained_duration_seconds` (default 60s) of CPU above `cpu_threshold_percent` (default 85%).
- **Expected diagnosis**: sustained CPU saturation, confidence medium, explicitly **not safe to automate** (a restart does not fix sustained load).
- **Remediation permitted**: no (rule disallows it entirely).
- **Expected notification**: yes.
- **Verify**: `guardian containers` shows CPU% climbing; `curl localhost:8420/metrics | grep cpu` is not exposed directly (CPU is per-incident evidence, not a standalone gauge) -- check `guardian incident show <id>`.
- **Cleanup**: `docker-compose -f deploy/docker-compose.yml stop demo-cpu-stress`.

### 5. Memory approaches threshold / grows continuously

- **Simulated**: a container that allocates 10 MB of tmpfs every 5 seconds.
- **Container**: `guardian-demo-memory-growth` (`mem_limit: 256m`).
- **Expected incidents**: `memory_growth` (low severity) once memory rises for `memory_growth_intervals` consecutive samples, then `memory_sustained_high` (high severity) once it sustains above `memory_threshold_percent` of the 256 MB limit.
- **Expected diagnosis**: possible leak (low confidence) escalating to sustained pressure (medium confidence) as it approaches the limit.
- **Remediation permitted**: no for both rules.
- **Expected notification**: yes for both, independently.
- **Verify**: `guardian containers --json | jq '.[] | select(.name=="guardian-demo-memory-growth")'`.
- **Cleanup**: `docker-compose -f deploy/docker-compose.yml restart demo-memory-growth`.

### 6. Restart loop is detected

- **Simulated**: a container that exits immediately and Docker's `restart: always` policy keeps restarting it.
- **Container**: `guardian-demo-crashloop`.
- **Expected incident**: `restart_loop`, severity critical, once `restart_count_threshold` (default 3) restarts occur within `restart_window_seconds` (default 300s).
- **Expected diagnosis**: crash-looping, confidence high, explicitly **not safe to automate** -- "repeated restarts will not fix a startup-time failure".
- **Remediation permitted**: no (rule disallows it, independent of allowlist).
- **Expected notification**: yes.
- **Verify**: `docker inspect guardian-demo-crashloop --format '{{.RestartCount}}'` climbs; `guardian incident show <id>` shows diagnosis recommending escalation, not restart.
- **Cleanup**: `docker-compose -f deploy/docker-compose.yml stop demo-crashloop`.

### 7. Docker daemon becomes unavailable

- **Simulated**: the monitoring process cannot reach the Docker API at all.
- **Container**: none -- this is a monitoring-infrastructure incident, raised against the pseudo-container `__guardian_system__`.
- **How to trigger safely**: run Guardian *outside* Docker (`guardian start`) and then temporarily point `docker.socket_url` in `config/guardian.yaml` at a nonexistent path, or (if Guardian runs as a container) briefly stop the host's Docker service -- **do not do this if other workloads on the host depend on Docker being up**; the safest repeatable way is the unit/integration tests in `tests/unit/test_evaluator.py::TestDaemonReachability`, which simulate this without touching a real daemon.
- **Expected incident**: `docker_daemon_unreachable`, severity critical, after `collection_failure_threshold` consecutive failed pings.
- **Expected diagnosis**: monitoring-infrastructure failure, confidence high, explicitly not a container remediation case.
- **Remediation permitted**: no.
- **Expected notification**: yes, immediately once the threshold is hit.
- **Verify**: `guardian incidents list` shows the incident against `__guardian_system__`.
- **Cleanup**: restore Docker/the socket path; the incident resolves once reachability returns.

### 8. Notification delivery fails

- **Simulated**: the webhook or SMTP endpoint is unreachable.
- **How to trigger safely**: enable the webhook channel pointed at an address that refuses connections, e.g. `http://127.0.0.1:1` in `config/guardian.yaml`'s `notifications.webhook`.
- **Expected behavior**: `NotificationManager` retries with exponential backoff (`max_retries`/`retry_backoff_seconds`), then records the attempt as `failed` -- it never raises and never crashes the monitoring cycle.
- **Expected notification record**: `guardian incidents list` + `curl localhost:8420/api/v1/incidents/<id>` shows the incident; the notification outcome itself is visible via `tests/unit/test_notifications.py::TestNotificationManager::test_retries_with_backoff_then_marks_failed`, which is the repeatable, deterministic version of this scenario.
- **Cleanup**: disable the webhook channel again.

### 9. Automated remediation is denied by policy

- **Simulated**: an incident that *would* be eligible for restart under the rule, but the static policy denies it.
- **Container**: any demo container, with `remediation.enabled: false` (the shipped default) or with the container left out of `restart_allowlist`.
- **Expected behavior**: `guardian remediation dry-run <container>` reports `NOT ELIGIBLE` with the exact denial reason (monitoring-only mode, empty allowlist, not allowlisted, protected, cooldown, or max actions reached).
- **Expected audit record**: every denial is still persisted via `guardian remediation history` with `outcome=denied` and the human-readable reason.
- **Verify**: `guardian remediation dry-run guardian-demo-unhealthy`.

### 10. Remediation is attempted but recovery verification fails

- **Simulated**: a restart is issued, but the container does not come back healthy within the grace period.
- **How to trigger safely**: enable remediation for `guardian-demo-crashloop` specifically --
  but note `restart_loop`'s rule itself disallows remediation, so to see a
  *verification failure* rather than a denial, point `restart_allowlist` at
  `guardian-demo-unhealthy` (whose healthcheck always fails, so it never
  reports healthy after a restart), set `remediation.enabled: true`,
  `remediation.dry_run: false`, then restart the `guardian` service.
- **Expected incident transition**: `ACTION_APPROVED` -> `REMEDIATING` -> `VERIFYING` -> `REMEDIATION_FAILED` (never silently marked resolved).
- **Expected audit record**: `guardian remediation history --container guardian-demo-unhealthy` shows `outcome=failed`, `recovery_verified=False`, with the detail explaining what was actually observed.
- **Cleanup**: set `remediation.enabled: false` again and restart the `guardian` service.

---

## Running these scenarios repeatably in CI / without touching real Docker

Most of the above have a deterministic, instant unit-test equivalent that
asserts the exact same behavior without timing-dependent `sleep`s or a real
Docker daemon:

| Scenario | Deterministic test |
|---|---|
| 1, 2 | `tests/unit/test_evaluator.py::TestUnexpectedStop`, `TestHealthUnhealthy` |
| 3 | `tests/unit/test_evaluator.py::TestStartupHealthTimeout` |
| 4, 5 | `tests/unit/test_evaluator.py::TestSustainedResourceRules`, `TestMemoryGrowth` |
| 6 | `tests/unit/test_evaluator.py::TestRestartLoop`, `tests/unit/test_remediation_engine.py::TestRulePermission` |
| 7 | `tests/unit/test_evaluator.py::TestDaemonReachability` |
| 8 | `tests/unit/test_notifications.py::TestNotificationManager` |
| 9 | `tests/unit/test_remediation_engine.py` (every `TestXxx::test_denied_*`) |
| 10 | `tests/unit/test_remediation_engine.py::TestSuccessfulRestart::test_recovery_not_verified_marks_remediation_failed` |

The live-Docker versions above (and `tests/e2e/`) exist to prove the same
logic holds against a real daemon, not to replace the fast, deterministic
suite. Three scenarios additionally have a **live, non-mocked** proof
against a real Docker daemon, with real `dry_run=false` restarts and real
recovery polling (not a fake collector): `tests/e2e/test_live_docker.py`'s
`TestLiveRestartIsActuallyIssuedAndRecoveryVerified` (scenario 9's success
case -- an eligible container genuinely gets restarted and its recovery is
genuinely verified), `TestLiveRemediationFailureWhenHealthNeverRecovers`
(scenario 10), and `TestRestartLoopIsDeniedEvenWhenFullyArmed` (scenario 6,
with remediation fully enabled and the container allowlisted, not just
monitoring-only). Run with `GUARDIAN_RUN_E2E=1 pytest tests/e2e -q`.
