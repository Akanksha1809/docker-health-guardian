# Interview Guide

Likely DevOps/SRE interview questions this project is meant to let you
answer concretely, with a pointer to the actual code/test backing each
answer.

## "Walk me through what happens when a container becomes unhealthy."

`Scheduler.run_once()` (`scheduler.py`) collects a `ContainerSnapshot` via
`DockerCollector`, passes it to `HealthEvaluator.evaluate()`
(`detection/evaluator.py`), which sees `health_status == UNHEALTHY` and
returns a `RuleMatch` for `health_unhealthy`. `IncidentManager.process_triggered()`
(`incidents/manager.py`) checks for an existing open incident on this
container+rule (dedup) or creates a new one, immediately computing a
`Diagnosis` via `diagnosis/engine.py`'s deterministic mapping. The incident
is persisted, a notification goes out through every enabled channel
(subject to severity/cooldown filtering), and `RemediationEngine` evaluates
whether a restart is eligible -- in the shipped default config, it's
denied because `remediation.enabled` is `false`. On a later cycle, once the
evaluator observes the health status is no longer `unhealthy`, the incident
is resolved.

## "How do you avoid false positives from a brief CPU spike?"

`HealthEvaluator` tracks a `cpu_exceeded_since` timestamp per container; a
breach only becomes a `RuleMatch` once `now - cpu_exceeded_since >=
sustained_duration_seconds` (default 60s), and the timestamp resets to
`None` the instant the metric drops back below threshold. See
`tests/unit/test_evaluator.py::TestSustainedResourceRules` for the exact
behavior, including a spike-then-drop case that intentionally never fires.

## "How do you know a restart actually fixed anything?"

It doesn't trust the Docker API's success response. After calling
`container.restart()`, `remediation/verifier.py`'s `verify_recovery()`
polls the container's real state/health for up to
`recovery_grace_period_seconds`. If it never reports
running+healthy(-or-no-healthcheck) in that window, the attempt is recorded
as `outcome=failed, recovery_verified=false` and the incident moves to
`REMEDIATION_FAILED` -- never silently reported as resolved. See
`tests/unit/test_remediation_engine.py::TestSuccessfulRestart`.

## "How do you prevent a crash-looping container from being restarted forever?"

Two independent layers: the `restart_loop` rule itself has
`remediation_permitted=False` in the rule catalog (`detection/rules.py`), so
once three+ restarts happen in the window, remediation is denied outright
regardless of allowlist. Separately, even rules that *do* permit
remediation are capped by `max_actions_per_container`, counted
per-incident via `count_attempts_for_incident`, so a problem that keeps
failing after N real attempts stops being retried and is left for a human.

## "What's your blast radius if this tool is misconfigured or compromised?"

Answered honestly in `docs/SECURITY.md`: Docker socket access is
effectively host root, full stop -- no amount of in-app safety logic
changes that. What the *application code* does within that access is
narrow (list/inspect/stats/logs/restart/events only, never create/remove/
exec/prune), and the one mutating call is behind the full eligibility chain
in `docs/REMEDIATION_POLICIES.md`. I'd say this plainly in an interview
rather than overstate the tool's own sandboxing.

## "How did you validate the CPU/memory math instead of guessing a formula?"

Followed Docker's own documented `cpu_stats`/`precpu_stats` delta formula
(the same one `docker stats` uses internally), and handled the
documented edge cases explicitly: zero system-delta on the first sample
(returns "unavailable," not zero), a negative delta from a counter reset,
and the cgroup v1 vs v2 difference in where cache/inactive-file memory is
reported (`memory_stats.stats.cache` vs `.inactive_file`), subtracting it
so "usage" matches what `docker stats` shows a human. See
`collector/stats.py`'s module docstring and
`tests/unit/test_stats.py` for synthetic-payload coverage of every branch.

## "Why is this a single-threaded polling loop instead of an event-driven/async design?"

Docker's own `stats`/`inspect` API is itself poll-based (there's no push
notification for "CPU just crossed 85%"), and a single non-overlapping loop
makes "is a cycle currently running" trivially answerable, which matters
for the no-concurrent-remediation guarantee and for keeping the SQLite
access pattern simple (short-lived connections, no cross-thread
contention). The tradeoff is reaction latency bounded by
`polling_interval_seconds`, which is an explicit, documented, and
configurable choice, not an accidental limitation.

## "What would you add if you had another sprint?"

Honest answer, not aspirational: authentication on the API if it's ever
bound beyond localhost; a retry/backoff on the Docker API calls themselves
(today a single transient failure in one cycle is just logged and retried
next cycle, which is simple but means brief daemon hiccups surface as a
`metrics_unreliable`/`docker_daemon_unreachable` incident rather than being
silently absorbed); and an automatic retention-cleanup scheduler instead of
the current manual `guardian cleanup` CLI invocation.
