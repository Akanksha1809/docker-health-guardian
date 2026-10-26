# Security Model

## The Docker socket is the whole threat model

**Anything with access to the Docker socket has effective root on the
host.** The Docker API can create a container with `--privileged`, bind-mount
`/`, or run as any UID -- there is no meaningful sandbox between "can call
the Docker API" and "can do anything the host's root user can do." This is
true regardless of:

- whether the calling process inside the container runs as root or not
  (Guardian's own container runs as a non-root `guardian` user, which is
  defense-in-depth against *other* vulnerability classes -- e.g. a future
  dependency RCE -- but it does **not** reduce what the mounted socket
  itself grants);
- whether the socket is reached from inside a container or from a bare host
  process.

**Practical implication**: only run Guardian where you already trust
whoever can modify its configuration or its container image, exactly as you
would for any other process with Docker socket access (a CI runner, a
reverse proxy that manages containers, etc.).

## What Guardian actually does with that access

Within that broad capability, Guardian's own code is deliberately narrow:

- It only ever calls `list`, `inspect`, `stats`, `logs`, `restart`, and
  `events` on the Docker API (`collector/docker_client.py`,
  `remediation/engine.py`). It never calls `create`, `remove`, `exec`,
  `prune`, or anything that touches images, volumes, or the daemon's own
  configuration.
- `restart` is the only mutating call, and it is gated by the full
  eligibility chain in `docs/REMEDIATION_POLICIES.md` -- monitoring-only by
  default, explicit allowlist, cooldowns, max-actions, and dry-run.
- Logs are pulled as a bounded tail (`docker.log_tail_lines`, default 50
  lines), not streamed or pulled in full, limiting both performance impact
  and the chance of incidentally capturing large amounts of log data.

## API exposure

The FastAPI app (`api/app.py`) binds to `127.0.0.1` by default in
`config/guardian.yaml` -- deliberately not reachable from the network
without an explicit operator change. It is also read-only: every endpoint
is a `GET`, and nothing it exposes can trigger a restart, change
configuration, or otherwise mutate state. There is no authentication layer
today, which is consistent with "localhost-only by default"; **if you change
`api.host` to bind beyond localhost, you are responsible for adding your own
authentication and network controls (a reverse proxy with auth, a firewall
rule, a VPN/private network) -- this project does not provide any.**

The Docker Compose deployment (`config/guardian.docker.yaml`) overrides
`api.host` to `0.0.0.0`. This is **not** a security downgrade in that
context: a container's own loopback interface is unreachable from the host
or from other containers regardless of the bind address, so the actual
access boundary there is Docker's own port publishing
(`ports: ["8420:8420"]` in `docker-compose.yml`) -- remove that line to make
the API reachable only from other containers on the Compose network, not
from the host at all.

## Secrets

- SMTP credentials and the webhook HMAC secret are **only** ever read from
  environment variables (`GUARDIAN_SMTP_USER`, `GUARDIAN_SMTP_PASSWORD`,
  `GUARDIAN_WEBHOOK_SECRET`) -- see `config/loader.py`'s `_ENV_OVERRIDES`.
  They are excluded from Pydantic's serialization (`exclude=True` on those
  fields), so a `model_dump()` of the config (e.g. for debugging or an API
  response) never leaks them.
- `.env` is git-ignored; `.env.example` documents the three variables
  without values.
- Nothing in this project logs a secret value. If you add a new secret,
  follow the same pattern: environment-only, `exclude=True` on the Pydantic
  field.

## Least privilege in deployment

- The Dockerfile creates and runs as a non-root `guardian` user.
- The Docker socket is mounted **read-only** (`:ro`) in
  `deploy/docker-compose.yml`. The Docker API itself doesn't distinguish
  "read-only" socket access in a security-relevant way (see above -- any
  API access is powerful), but mounting read-only still blocks one class of
  mistake: Guardian's own code accidentally writing to the socket path.
- Guardian never restarts the Docker daemon itself, never modifies host
  files outside its own SQLite database path, and never requires any
  capability beyond `NET_BIND_SERVICE`-equivalent (binding its own API
  port) and Docker socket access.

## What this project deliberately does not try to solve

- **Multi-tenant isolation**: Guardian assumes whoever configures it is
  also the one entitled to restart the containers it's pointed at. There is
  no per-user authorization model.
- **Supply-chain verification** of the images it monitors or restarts --
  that's a separate concern (image scanning, signing) outside this tool's
  scope.
- **Detecting malicious containers** -- Guardian is a reliability tool, not
  a security scanner.
