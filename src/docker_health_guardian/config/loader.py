"""Loads GuardianAppConfig from YAML with environment-variable overrides for secrets."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import ValidationError

from .models import GuardianAppConfig

DEFAULT_CONFIG_PATH = "config/guardian.yaml"

# Secret fields are never read from YAML; they come only from the environment.
_ENV_OVERRIDES = {
    ("notifications", "email", "smtp_user"): "GUARDIAN_SMTP_USER",
    ("notifications", "email", "smtp_password"): "GUARDIAN_SMTP_PASSWORD",
    ("notifications", "webhook", "secret"): "GUARDIAN_WEBHOOK_SECRET",
}


class ConfigError(Exception):
    """Raised when configuration fails to load or validate."""


def _set_nested(data: dict, path: tuple[str, ...], value: str) -> None:
    node = data
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


def load_config(path: str | Path | None = None) -> GuardianAppConfig:
    """Load, override with env secrets, and validate the application config.

    Raises ConfigError with a readable message on any validation failure --
    invalid thresholds, negative intervals, malformed notification settings,
    or contradictory safety settings are all rejected here.
    """
    config_path = Path(path or DEFAULT_CONFIG_PATH)
    raw: dict = {}
    if config_path.exists():
        try:
            loaded = yaml.safe_load(config_path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Failed to parse YAML config at {config_path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError(f"Config file {config_path} must contain a YAML mapping")
        raw = loaded

    for field_path, env_var in _ENV_OVERRIDES.items():
        env_value = os.environ.get(env_var)
        if env_value:
            _set_nested(raw, field_path, env_value)

    try:
        return GuardianAppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"Invalid configuration:\n{exc}") from exc
