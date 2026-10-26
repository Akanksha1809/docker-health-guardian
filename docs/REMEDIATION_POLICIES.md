# Remediation Policies

This is the safety contract for the only code path that can mutate a live
container (`remediation/engine.py`'s `RemediationEngine.attempt_remediation`).
Every rule below is enforced in code and covered by a test in
`tests/unit/test_remediation_engine.py` -- the table links each policy to
its test class so the claim is checkable, not just asserted here.

| Policy | Enforced by | Test |
|---|---|---|
| Monitoring-only by default | `RemediationConfig.enabled` defaults to `false` | `TestMonitoringOnlyIsSafeByDefault` |
| Dry-run never mutates | `cfg.dry_run` short-circuits before any Docker API call | `TestDryRunNeverMutatesLiveContainer` (e2e), `TestDryRunNeverMutates` (unit) |
| Rule must permit remediation | `RuleDefinition.remediation_permitted` (e.g. `restart_loop` and all resource-pressure rules are `False`) | `TestRulePermission` |
| Explicit allowlist, empty = nothing eligible | `restart_allowlist` checked before any restart | `TestAllowlistEnforcement` |
| Protected containers always win | checked unconditionally, before the allowlist and before any CLI override | `TestAllowlistEnforcement::test_denied_when_protected`, `TestContainerOverrides::test_enable_override_does_not_bypass_protected_containers` |
| Per-container CLI override narrows/widens but never bypasses protection | `Database.get_container_override` consulted after `protected_containers` | `TestContainerOverrides` |
| Cooldown between real attempts | last non-dry-run attempt's timestamp vs `cooldown_seconds` | `TestCooldownAndMaxActions::test_cooldown_blocks_immediate_second_attempt` |
| Bounded retries per incident | `count_attempts_for_incident(dry_run=False)` vs `max_actions_per_container` | `TestCooldownAndMaxActions::test_max_actions_per_container_blocks_further_attempts` |
| Dry-run attempts don't consume the real budget | counted separately by `dry_run` flag | `TestCooldownAndMaxActions::test_dry_run_attempts_do_not_count_toward_max_actions` |
| No concurrent restarts of the same container | in-process `self._in_progress` set | `TestConcurrencyLock` |
| Every attempt is audited, denied or not | `Database.upsert_remediation_attempt` called on every path | `TestEveryAttemptIsAudited` |
| Recovery is verified, not assumed | `verify_recovery()` polls real state/health after the restart call returns | `TestSuccessfulRestart` |
| Failed verification is recorded, not silently "fixed" | `IncidentState.REMEDIATION_FAILED`, never `RESOLVED`, set by the engine | `TestSuccessfulRestart::test_recovery_not_verified_marks_remediation_failed` |
| Restart command failure is recorded | `RemediationOutcome.FAILED` with the exception text | `TestSuccessfulRestart::test_restart_command_failure_is_recorded` |

## Why `RemediationEngine` never marks an incident `RESOLVED`

`RemediationEngine` can move an incident to `ACTION_APPROVED`,
`REMEDIATING`, `VERIFYING`, or `REMEDIATION_FAILED` -- but the only thing
that ever sets `RESOLVED` is `IncidentManager.process_cleared()`, called
from the scheduler only in response to the **evaluator observing real
recovery on a later cycle** (see `scheduler.py::_handle_triggered` and
`run_once`). This means "resolved" always means "Guardian subsequently
observed the condition was gone," never "we issued a command that is
supposed to fix it." A restart that succeeds at the Docker-API level but
whose container still fails its health check stays as `REMEDIATION_FAILED`
forever (or until a human intervenes) -- it is never reported as fixed.

## Why `max_actions_per_container` is per-incident, not lifetime

A container that has one incident, gets restarted twice, recovers, and
months later has an unrelated new incident should not be permanently
blacklisted from ever being restarted again. The counter resets because
it's scoped to `incident_id`
(`count_attempts_for_incident`), so a *new* incident (a new problem) gets a
fresh budget, while the *same* incident (the same ongoing problem) cannot
exceed its budget no matter how many monitoring cycles pass.

## Why some rules disallow remediation entirely

Three categories of problem have no restart-shaped fix, so the rule
definitions mark them `remediation_permitted=False` and the diagnosis
engine independently sets `safe_to_automate=False` for the same reasons
(two separate code paths agreeing, not one flag duplicated):

- **`restart_loop`**: if a container is already crash-looping, another
  restart cannot fix whatever makes it fail at startup. Diagnosis
  explicitly recommends escalation instead.
- **CPU/memory pressure rules**: sustained resource pressure is a capacity
  or configuration problem (or a real traffic increase), not a transient
  fault a restart resolves. Restarting would just interrupt a possibly
  legitimately busy workload.
- **`docker_daemon_unreachable`**: this is a monitoring-infrastructure
  failure, not a container failure -- there is no container-level action
  that applies.

## Extending this safely

A new remediation action (beyond `restart`) must:

1. Be added to the `ActionName` literal in `config/models.py`.
2. Go through the same eligibility chain (`check_eligibility`), not a
   parallel path.
3. Produce a `RemediationAttempt` record on every branch (denied, dry-run,
   success, failure) -- there is no "fire and forget" action in this
   codebase.
4. Have its own recovery-verification step appropriate to what it does, or
   explicitly document why verification doesn't apply (e.g. `capture_logs`
   has no "recovery" to verify).
