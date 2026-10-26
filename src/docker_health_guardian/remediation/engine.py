"""Policy-driven, safe remediation engine.

Safety model (enforced here, not optional):
- Monitoring-only by default: nothing happens unless `remediation.enabled`
  AND the rule itself permits remediation AND "restart" is in allowed_actions.
- Explicit allowlist: a container must be named in `restart_allowlist`; an
  empty allowlist means nothing is eligible. `protected_containers` always
  wins even if also allowlisted (rejected earlier, at config-validation
  time, by GuardianAppConfig).
- dry_run executes the full eligibility path and logs what *would* happen,
  but never calls the Docker API to mutate container state.
- Cooldown and max-actions-per-incident stop crash-loop restart storms.
- A per-container in-process lock prevents two concurrent restarts of the
  same container.
- Every attempt (eligible or not, dry-run or real) is persisted with its
  reason, so `guardian remediation history` is a complete audit trail.
- Recovery is verified; this engine never marks an incident RESOLVED itself
  -- that stays the health evaluator's job, based on real observed state on
  a later cycle. This engine only marks REMEDIATION_FAILED when verification
  does not succeed, so a human is not left thinking it was fixed.
"""

from __future__ import annotations

import logging

from docker_health_guardian.collector import DockerCollector
from docker_health_guardian.config.models import RemediationConfig
from docker_health_guardian.incidents import IncidentManager
from docker_health_guardian.models import (
    Incident,
    IncidentEvent,
    IncidentState,
    RemediationAttempt,
    RemediationOutcome,
    utcnow,
)
from docker_health_guardian.persistence import Database

from .verifier import verify_recovery

logger = logging.getLogger(__name__)

RESTART_ACTION = "restart"


class RemediationEngine:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._in_progress: set[str] = set()

    def check_eligibility(self, incident: Incident, cfg: RemediationConfig) -> tuple[bool, str]:
        from docker_health_guardian.detection.rules import get_rule

        rule = get_rule(incident.rule_id)
        if not rule.remediation_permitted:
            return False, f"rule '{incident.rule_id}' does not permit automated remediation"
        if not cfg.enabled:
            return False, "remediation is disabled (monitoring-only mode)"
        if RESTART_ACTION not in cfg.allowed_actions:
            return False, "'restart' is not in remediation.allowed_actions"
        if incident.container_name in cfg.protected_containers:
            return False, "container is in remediation.protected_containers"

        # A per-container override from `guardian remediation enable/disable`
        # can narrow or widen eligibility within the allowlist model, but can
        # never bypass protected_containers (checked above, unconditionally).
        override = self._db.get_container_override(incident.container_name)
        if override is False:
            return False, "remediation explicitly disabled for this container (CLI override)"
        if override is True:
            pass  # explicitly enabled -- skip the static allowlist checks below
        elif not cfg.restart_allowlist:
            return False, "remediation.restart_allowlist is empty -- no container is eligible"
        elif incident.container_name not in cfg.restart_allowlist:
            return False, "container is not in remediation.restart_allowlist"
        if incident.container_name in self._in_progress:
            return False, "a remediation action is already in progress for this container"

        recent = self._db.list_remediation_attempts(
            container_name=incident.container_name, limit=1
        )
        if recent and not recent[0].dry_run:
            elapsed = (utcnow() - recent[0].started_at).total_seconds()
            if elapsed < cfg.cooldown_seconds:
                return False, (
                    f"cooldown active ({round(elapsed, 1)}s of {cfg.cooldown_seconds}s "
                    "since the last attempt)"
                )

        attempt_count = self._db.count_attempts_for_incident(incident.incident_id, dry_run=False)
        if attempt_count >= cfg.max_actions_per_container:
            return False, (
                f"max_actions_per_container ({cfg.max_actions_per_container}) already "
                "reached for this incident"
            )
        return True, f"rule '{incident.rule_id}' permits restart and all safety checks passed"

    def attempt_remediation(
        self,
        incident: Incident,
        cfg: RemediationConfig,
        collector: DockerCollector,
        incident_manager: IncidentManager,
    ) -> RemediationAttempt:
        eligible, reason = self.check_eligibility(incident, cfg)

        if not eligible:
            attempt = RemediationAttempt(
                incident_id=incident.incident_id,
                container_name=incident.container_name,
                action=RESTART_ACTION,
                dry_run=cfg.dry_run,
                reason=reason,
                outcome=RemediationOutcome.DENIED,
            )
            attempt.finished_at = utcnow()
            self._db.upsert_remediation_attempt(attempt)
            self._db.add_event(
                IncidentEvent(
                    incident_id=incident.incident_id,
                    kind="action",
                    detail=f"Remediation denied: {reason}",
                )
            )
            return attempt

        if cfg.dry_run:
            attempt = RemediationAttempt(
                incident_id=incident.incident_id,
                container_name=incident.container_name,
                action=RESTART_ACTION,
                dry_run=True,
                reason=reason,
                outcome=RemediationOutcome.SKIPPED,
                detail="dry_run=true: eligibility confirmed but restart was not executed",
            )
            attempt.finished_at = utcnow()
            self._db.upsert_remediation_attempt(attempt)
            self._db.add_event(
                IncidentEvent(
                    incident_id=incident.incident_id,
                    kind="action",
                    detail=f"Dry-run: would restart ({reason})",
                )
            )
            return attempt

        self._in_progress.add(incident.container_name)
        try:
            return self._execute_restart(incident, cfg, collector, incident_manager, reason)
        finally:
            self._in_progress.discard(incident.container_name)

    def _execute_restart(
        self,
        incident: Incident,
        cfg: RemediationConfig,
        collector: DockerCollector,
        incident_manager: IncidentManager,
        reason: str,
    ) -> RemediationAttempt:
        incident_manager.transition(
            incident, IncidentState.ACTION_APPROVED, f"Remediation approved: {reason}"
        )
        attempt = RemediationAttempt(
            incident_id=incident.incident_id,
            container_name=incident.container_name,
            action=RESTART_ACTION,
            dry_run=False,
            reason=reason,
            outcome=RemediationOutcome.FAILED,
        )
        self._db.upsert_remediation_attempt(attempt)
        incident_manager.transition(
            incident, IncidentState.REMEDIATING, f"Restarting container {incident.container_name}"
        )

        try:
            collector.restart_container(incident.container_name)
        except Exception as exc:  # noqa: BLE001 -- any Docker SDK error is a failed attempt
            attempt.detail = f"restart command failed: {exc}"
            attempt.finished_at = utcnow()
            self._db.upsert_remediation_attempt(attempt)
            incident_manager.transition(
                incident, IncidentState.REMEDIATION_FAILED, attempt.detail
            )
            logger.warning(
                "restart failed for %s (incident %s): %s",
                incident.container_name,
                incident.incident_id,
                exc,
            )
            return attempt

        incident_manager.transition(
            incident, IncidentState.VERIFYING, "Restart issued, verifying recovery"
        )
        recovered, detail = verify_recovery(
            collector, incident.container_name, cfg.recovery_grace_period_seconds
        )
        attempt.recovery_verified = recovered
        attempt.outcome = RemediationOutcome.SUCCESS if recovered else RemediationOutcome.FAILED
        attempt.detail = detail
        attempt.finished_at = utcnow()
        self._db.upsert_remediation_attempt(attempt)
        self._db.add_event(
            IncidentEvent(incident_id=incident.incident_id, kind="verification", detail=detail)
        )

        if not recovered:
            incident_manager.transition(incident, IncidentState.REMEDIATION_FAILED, detail)
            logger.warning(
                "recovery not verified for %s (incident %s): %s",
                incident.container_name,
                incident.incident_id,
                detail,
            )
        else:
            logger.info(
                "recovery verified for %s (incident %s)",
                incident.container_name,
                incident.incident_id,
            )
        return attempt
