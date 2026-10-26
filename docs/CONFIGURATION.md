# Configuration Reference

Guardian reads `config/guardian.yaml` by default (`--config` overrides the
path on every CLI command). All settings are validated at startup by
Pydantic models in `src/docker_health_guardian/config/models.py`; invalid
values fail fast with a readable error (`guardian config-validate` runs
just this check).

## `guardian`

| Key | Default | Notes |
|---|---|---|
| `polling_interval_seconds` | `15` | Must be `> 0`. Time between monitoring cycles. |
| `mode` | `monitoring` | `monitoring` or `remediation` -- currently informational/logged; the actual safety gate is `remediation.enabled`. |
| `log_level` | `INFO` | One of `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`. |
| `cycle_timeout_seconds` | `30` | A cycle exceeding this logs a warning (does not abort the cycle). |

## `detection`

| Key | Default | Notes |
|---|---|---|
| `cpu_threshold_percent` | `85` | `0 < x <= 100`. |
| `memory_threshold_percent` | `90` | `0 < x <= 100`. |
| `sustained_duration_seconds` | `60` | How long a CPU/memory breach must persist before it becomes an incident. |
| `restart_window_seconds` | `300` | Rolling window for the restart-loop rule. |
| `restart_count_threshold` | `3` | Restarts within the window needed to trigger `restart_loop`. |
| `stopped_grace_period_seconds` | `30` | How long a container may stay stopped before `stopped_beyond_grace` fires. |
| `startup_health_grace_seconds` | `60` | How long a container may stay in Docker's `starting` health state. |
| `memory_growth_intervals` | `5` | Consecutive rising samples needed to flag `memory_growth`. |
| `collection_failure_threshold` | `3` | Consecutive unreliable-metric samples, or consecutive failed daemon pings, before raising `metrics_unreliable` / `docker_daemon_unreachable`. |

## `remediation`

**Security-relevant: these settings control the only code path that can
mutate a live container.** See `docs/REMEDIATION_POLICIES.md` for the full
safety model.

| Key | Default | Notes |
|---|---|---|
| `enabled` | `false` | Master switch. `false` = monitoring-only, nothing is ever restarted. |
| `dry_run` | `true` | When `true`, every eligibility check still runs and is logged, but the restart is never executed. |
| `cooldown_seconds` | `300` | Minimum time between real restart attempts on the same container. |
| `max_actions_per_container` | `2` | Cap on real restart attempts **per incident** (not lifetime) -- stops endless retries of the same crash. |
| `allowed_actions` | `[capture_logs, restart]` | `restart` must be present for any restart to ever be eligible. |
| `restart_allowlist` | `[]` | Containers eligible for restart. **Empty means nothing is eligible**, regardless of any other setting. |
| `protected_containers` | `[]` | Always denied, even if also in the allowlist (config validation rejects that contradiction outright) or enabled via a CLI override. |
| `recovery_grace_period_seconds` | `20` | How long to poll for real recovery after issuing a restart before giving up and marking `REMEDIATION_FAILED`. |

Config validation rejects `enabled: true` + `dry_run: false` with an empty
`allowed_actions`, and rejects any container appearing in both
`restart_allowlist` and `protected_containers`.

A per-container override on top of the static config is available via
`guardian remediation enable/disable <container>` (persisted in SQLite, not
the YAML file) -- it can narrow or widen eligibility within the allowlist
model, but can never bypass `protected_containers`.

## `docker`

| Key | Default | Notes |
|---|---|---|
| `monitored_containers` | `[]` | Empty = monitor every container the socket can see. Non-empty = only these. |
| `excluded_containers` | `[]` | Always excluded, even if also in `monitored_containers`. |
| `socket_url` | `null` | Override the Docker SDK's auto-detected connection (`docker.from_env()`). |
| `api_timeout_seconds` | `10` | Timeout for Docker API calls. |
| `log_tail_lines` | `50` | Bounded log excerpt size used for diagnosis/incident investigation -- full logs are never pulled every cycle. |

A container can also be excluded per-container via the label
`guardian.monitoring=disabled`, independent of this config file.

## `notifications`

| Key | Default | Notes |
|---|---|---|
| `console.enabled` | `true` | Always-available channel; logs a structured summary. |
| `email.enabled` | `false` | Requires `smtp_host`, `from_addr`, `to_addrs` in YAML. **Credentials come only from environment variables** (`GUARDIAN_SMTP_USER`/`GUARDIAN_SMTP_PASSWORD`), never from the YAML file. |
| `webhook.enabled` | `false` | Requires `url` in YAML. An optional HMAC-SHA256 signature uses `GUARDIAN_WEBHOOK_SECRET` (env only). |
| `min_severity` | `low` | Incidents below this severity are never sent to any channel. |
| `cooldown_seconds` | `300` | Per-incident-per-channel cooldown to prevent notification storms. |
| `max_retries` | `3` | Bounded retry count on transient delivery failure. |
| `retry_backoff_seconds` | `2` | Doubles on each retry (exponential backoff). |

## `persistence`

| Key | Default | Notes |
|---|---|---|
| `db_path` | `data/guardian.db` | SQLite file; parent directories are created automatically. |
| `retention_days` | `30` | `guardian start` does not run cleanup automatically -- run `guardian cleanup` periodically (e.g. via cron) to prune old **resolved/suppressed** incidents only; open incidents are never removed regardless of age. |

## `api`

| Key | Default | Notes |
|---|---|---|
| `enabled` | `true` | If `false`, `guardian start` runs only the monitoring loop. |
| `host` | `127.0.0.1` | **Security-relevant** -- see `docs/SECURITY.md`. The Docker Compose deployment overrides this to `0.0.0.0` in `config/guardian.docker.yaml` because container loopback is unreachable from outside the container; the actual access boundary there is Docker's own port publishing. |
| `port` | `8420` | |

## Environment variable overrides

Only secrets are read from the environment, and only these three:

- `GUARDIAN_SMTP_USER`
- `GUARDIAN_SMTP_PASSWORD`
- `GUARDIAN_WEBHOOK_SECRET`

Everything else is YAML-only, by design -- so `guardian config-validate`
against a given file is a complete picture of behavior (minus secrets).
