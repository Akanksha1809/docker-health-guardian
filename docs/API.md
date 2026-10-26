# API Reference

Base URL: `http://127.0.0.1:8420` by default (see `docs/SECURITY.md` for the
bind-address rationale). Every endpoint is read-only (`GET`); nothing here
can create, restart, or remove a container, or change configuration.

## `GET /health`

Liveness check. Always returns `200` if the process is up.

```json
{"status": "ok"}
```

## `GET /ready`

Readiness check: confirms the SQLite database is initialized and queryable.
Returns `503` with a `detail` message if not.

```json
{"status": "ready"}
```

## `GET /api/v1/containers`

Live snapshot of every container Guardian's Docker collector can currently
see (subject to `monitored_containers`/`excluded_containers`). Returns
`503` if the Docker daemon is unreachable.

Response: a JSON array of `ContainerSnapshot` objects --
`container_id`, `name`, `image`, `state`, `health_status`, `started_at`,
`exit_code`, `restart_count`, `labels`, `stats` (cpu/memory/network/blkio,
each possibly `null` with an `unavailable_reason` rather than a misleading
`0`), `observed_at`, `monitoring_excluded`.

## `GET /api/v1/incidents`

Paginated, filterable incident list.

| Query param | Type | Default | Notes |
|---|---|---|---|
| `state` | string | none | e.g. `investigating`, `resolved` |
| `severity` | string | none | `low`/`medium`/`high`/`critical` |
| `container_name` | string | none | exact match |
| `limit` | int | `50` | `1..500` |
| `offset` | int | `0` | `>= 0` |

```json
{
  "items": [ { "incident_id": "...", "container_name": "...", "...": "..." } ],
  "total": 3,
  "limit": 50,
  "offset": 0
}
```

`total` reflects the filtered count (matching `state`/`severity`), not the
page size.

## `GET /api/v1/incidents/{incident_id}`

Full detail for one incident: the incident itself, its complete event
timeline, and every remediation attempt made against it. `404` if the ID
doesn't exist.

```json
{
  "incident": { "...": "..." },
  "events": [ { "kind": "state_change", "detail": "Incident detected", "...": "..." } ],
  "remediation_attempts": [ { "outcome": "denied", "reason": "...", "...": "..." } ]
}
```

## `GET /api/v1/remediation/history`

| Query param | Type | Default |
|---|---|---|
| `container_name` | string | none |
| `limit` | int | `50` (1..500) |
| `offset` | int | `0` |

```json
{"items": [ { "action": "restart", "dry_run": true, "outcome": "skipped", "...": "..." } ], "limit": 50, "offset": 0}
```

## `GET /metrics`

Prometheus text-format exposition. See `docs/CONFIGURATION.md` and
`src/docker_health_guardian/metrics/registry.py` for the full metric list;
all label values are drawn from small fixed sets (state, severity,
outcome) -- never container or incident IDs.

## Error format

FastAPI's default `HTTPException` shape is used consistently:

```json
{"detail": "incident abc123 not found"}
```

## What's intentionally not here

No endpoint can trigger a restart, change `remediation.enabled`, or modify
the allowlist -- those are CLI-only (`guardian remediation enable/disable`,
and editing `config/guardian.yaml` directly), by design, to keep the API
surface's blast radius at "read everything, change nothing."
