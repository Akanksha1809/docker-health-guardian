# Troubleshooting

## `guardian self-check` fails on "docker daemon not reachable"

- Confirm Docker is actually running: `docker ps`.
- If you use Colima (as this project was developed and tested against),
  confirm the context: `docker context ls` should show `colima` (or
  whichever context you use) as current (`*`).
- Check the socket path Guardian will use: it calls `docker.from_env()`
  unless `docker.socket_url` is set in config, which respects
  `DOCKER_HOST`/`DOCKER_CONTEXT` the same way the `docker` CLI does.

## Docker socket permission denied inside the container

```
DockerException: Error while fetching server API version: ('Connection aborted.', PermissionError(13, 'Permission denied'))
```

This means the `guardian` user inside the container doesn't have
permission to open the mounted `/var/run/docker.sock`. The container's
`DOCKER_GID` build argument must match the **GID that owns the socket as
seen from inside a Linux container on this host** -- not what `stat` reports
on macOS, which describes the bind-mount source, not the Colima VM's view.
Detect the correct value and rebuild:

```bash
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock busybox:1.36 stat -c '%g' /var/run/docker.sock
# then:
DOCKER_GID=<that number> docker-compose -f deploy/docker-compose.yml up -d --build
```

(This exact sequence was needed and verified while building this project on
Colima/macOS -- the default `DOCKER_GID=999` guess was wrong there; the
actual value was `991`.)

## `guardian-demo-*` containers are affecting my other containers

They shouldn't -- every demo/e2e container is prefixed (`guardian-demo-` or
`guardian-e2e-`) and uses its own Compose project name (`guardian-demo`), so
`docker-compose -f deploy/docker-compose.yml down` only ever touches those.
If `docker.monitored_containers` is left empty, Guardian will still
*observe* (read-only: list/inspect/stats) every other container on the
host -- that's expected Docker API behavior, not a mutation. If you don't
want that noise in a demo, set `monitored_containers` explicitly (see
`config/guardian.docker.yaml`, which scopes the shipped demo to only its own
six containers).

## No incidents ever appear

- Check `guardian status` -- does it report containers observed > 0?
- Check `docker.monitored_containers`/`excluded_containers` -- an
  unintentionally narrow allowlist silently excludes everything else.
- Thresholds matter: `cpu_sustained_high` needs the CPU above threshold for
  the *entire* `sustained_duration_seconds` window, not just a spike. Use
  `guardian containers` repeatedly to watch the live values.
- A container without a configured `HEALTHCHECK` reports `health_status:
  none`, which is correct and will never trigger `health_unhealthy` --
  this is intentional (see `docs/ARCHITECTURE.md`), not a bug.

## Metrics endpoint shows stale/zero container-state gauges

`guardian_containers_by_state`/`guardian_monitored_containers` are updated
by the scheduler each cycle, not recomputed on every scrape. If no
scheduler cycle has run yet in this process (e.g. you started only the API
without `guardian start`), they'll read zero. `guardian_open_incidents` and
`guardian_incidents_by_severity`, by contrast, are recomputed directly from
SQLite on every scrape, so they're always current.

## Notification channel always fails

- **Email**: confirm `GUARDIAN_SMTP_USER`/`GUARDIAN_SMTP_PASSWORD` are set
  in the environment (never in YAML) and that `smtp_host`/`smtp_port` match
  your provider. `EmailChannel` reports the exact `smtplib` exception text
  in the notification record's `detail` field (`guardian incident show
  <id>` or `GET /api/v1/incidents/{id}`).
- **Webhook**: confirm the URL is reachable from wherever Guardian runs
  (inside a container, "localhost" means the container's own loopback --
  the same caveat as the API's bind address, see `docs/SECURITY.md`).

## Tests are slow or hang

- `tests/unit` and `tests/integration` should complete in ~1-2 seconds with
  zero real Docker calls or real `time.sleep`s (everything injectable is
  injected with a fake clock/sleep in tests). If a test hangs, it's a sign
  a mock wasn't actually used -- see `tests/integration/test_docker_collector.py`'s
  note about the `DockerCollector.__init__` precedence bug found during
  this project's own Phase 8, which caused exactly this symptom.
- `tests/e2e` is opt-in (`GUARDIAN_RUN_E2E=1`) and legitimately takes
  ~60-70 seconds because it waits on real Docker healthcheck/restart
  timing -- that's expected, not a hang.

## "Resource temporarily unavailable" / timeouts pulling `busybox:1.36`

This was a one-off transient registry issue encountered once in this
project's own development session, unrelated to Guardian itself -- retrying
`docker pull busybox:1.36` resolved it immediately.

## `git push` fails with `HTTP 408` / "unexpected disconnect while reading sideband packet"

Encountered while first publishing this repo: a normal `git push` carrying
the demo GIF/cast (under 1.2MB total) consistently failed this way from two
different machines/networks, even after increasing
`http.postBuffer`/`http.lowSpeedTime` and even for much smaller unrelated
binary test payloads -- pointing at something in GitHub's push-processing
path for that content rather than plain client bandwidth. Things worth
trying, roughly in order: `git config http.version HTTP/1.1` then retry;
push over SSH instead of HTTPS; or, as done here, stop pushing large
binaries as git blobs entirely and upload them as a GitHub Release asset
(`gh release create <tag> <files...>`) instead, which uses a different
upload path and sidestepped the issue completely. See
`assets/` + the README's Demo section for how this project references the
release-hosted files instead of a repo-relative path.
