"""Pydantic configuration models for Docker Health Guardian.

All settings are validated at startup. Secrets (passwords, webhook URLs) are
never read from the YAML file in plaintext by convention -- they are loaded
from environment variables via `GuardianAppConfig.load()`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Severity = Literal["low", "medium", "high", "critical"]
GuardianMode = Literal["monitoring", "remediation"]
ActionName = Literal[
    "capture_logs",
    "restart",
    "stop_automation",
    "recheck_status",
    "wait_and_verify",
    "escalate",
]


class GuardianConfig(BaseModel):
    polling_interval_seconds: float = Field(default=15.0, gt=0)
    mode: GuardianMode = "monitoring"
    log_level: str = "INFO"
    cycle_timeout_seconds: float = Field(default=30.0, gt=0)

    @field_validator("log_level")
    @classmethod
    def _valid_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return v.upper()


class DetectionConfig(BaseModel):
    cpu_threshold_percent: float = Field(default=85.0, gt=0, le=100)
    memory_threshold_percent: float = Field(default=90.0, gt=0, le=100)
    sustained_duration_seconds: float = Field(default=60.0, ge=0)
    restart_window_seconds: float = Field(default=300.0, gt=0)
    restart_count_threshold: int = Field(default=3, ge=1)
    stopped_grace_period_seconds: float = Field(default=30.0, ge=0)
    startup_health_grace_seconds: float = Field(default=60.0, ge=0)
    memory_growth_intervals: int = Field(
        default=5, ge=2, description="Consecutive rising samples to flag a memory leak"
    )
    collection_failure_threshold: int = Field(
        default=3, ge=1, description="Consecutive collection failures before raising an incident"
    )


class RemediationConfig(BaseModel):
    enabled: bool = False
    dry_run: bool = True
    cooldown_seconds: float = Field(default=300.0, ge=0)
    max_actions_per_container: int = Field(default=2, ge=0)
    allowed_actions: list[ActionName] = Field(
        default_factory=lambda: ["capture_logs", "restart"]
    )
    restart_allowlist: list[str] = Field(default_factory=list)
    protected_containers: list[str] = Field(default_factory=list)
    recovery_grace_period_seconds: float = Field(default=20.0, ge=0)

    @model_validator(mode="after")
    def _check_consistency(self) -> RemediationConfig:
        if self.enabled and not self.dry_run and not self.allowed_actions:
            raise ValueError(
                "remediation.enabled=true with dry_run=false requires at least one "
                "allowed_action"
            )
        overlap = set(self.restart_allowlist) & set(self.protected_containers)
        if overlap:
            raise ValueError(
                f"containers cannot be both in restart_allowlist and protected_containers: "
                f"{sorted(overlap)}"
            )
        return self


class DockerConfig(BaseModel):
    monitored_containers: list[str] = Field(default_factory=list)
    excluded_containers: list[str] = Field(default_factory=list)
    socket_url: str | None = None
    api_timeout_seconds: float = Field(default=10.0, gt=0)
    log_tail_lines: int = Field(default=50, ge=1, le=1000)


class ConsoleNotificationConfig(BaseModel):
    enabled: bool = True


class EmailNotificationConfig(BaseModel):
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = Field(default=587, gt=0, le=65535)
    use_tls: bool = True
    from_addr: str = ""
    to_addrs: list[str] = Field(default_factory=list)
    smtp_user: str = Field(default="", exclude=True)
    smtp_password: str = Field(default="", exclude=True)

    @model_validator(mode="after")
    def _check_email(self) -> EmailNotificationConfig:
        if self.enabled and (not self.smtp_host or not self.from_addr or not self.to_addrs):
            raise ValueError(
                "email notifications enabled but smtp_host/from_addr/to_addrs incomplete"
            )
        return self


class WebhookNotificationConfig(BaseModel):
    enabled: bool = False
    url: str = ""
    timeout_seconds: float = Field(default=5.0, gt=0)
    secret: str = Field(default="", exclude=True)

    @model_validator(mode="after")
    def _check_webhook(self) -> WebhookNotificationConfig:
        if self.enabled and not self.url:
            raise ValueError("webhook notifications enabled but url is empty")
        return self


class NotificationsConfig(BaseModel):
    console: ConsoleNotificationConfig = Field(default_factory=ConsoleNotificationConfig)
    email: EmailNotificationConfig = Field(default_factory=EmailNotificationConfig)
    webhook: WebhookNotificationConfig = Field(default_factory=WebhookNotificationConfig)
    min_severity: Severity = "low"
    cooldown_seconds: float = Field(default=300.0, ge=0)
    max_retries: int = Field(default=3, ge=0)
    retry_backoff_seconds: float = Field(default=2.0, gt=0)


class PersistenceConfig(BaseModel):
    db_path: str = "data/guardian.db"
    retention_days: int = Field(default=30, ge=1)


class ApiConfig(BaseModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = Field(default=8420, gt=0, le=65535)


class GuardianAppConfig(BaseModel):
    guardian: GuardianConfig = Field(default_factory=GuardianConfig)
    detection: DetectionConfig = Field(default_factory=DetectionConfig)
    remediation: RemediationConfig = Field(default_factory=RemediationConfig)
    docker: DockerConfig = Field(default_factory=DockerConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
