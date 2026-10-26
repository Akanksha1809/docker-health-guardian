#!/usr/bin/env bash
# 5-10 minute live demonstration of Docker Health Guardian.
#
# Brings up the guardian-demo Compose stack (guardian + six disposable
# sample workloads), waits for incidents to accumulate, prints them via
# both the CLI and the API, then tears everything down. Never touches any
# container outside the `guardian-demo-` prefix.
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE="docker-compose -f deploy/docker-compose.yml"

echo "==> Detecting the Docker socket's GID inside a Linux container on this host..."
export DOCKER_GID
DOCKER_GID=$(docker run --rm -v /var/run/docker.sock:/var/run/docker.sock busybox:1.36 \
  stat -c '%g' /var/run/docker.sock)
echo "    DOCKER_GID=${DOCKER_GID}"

echo "==> Building and starting guardian + sample workloads..."
$COMPOSE up -d --build

echo "==> Waiting up to 90s for the API to come up..."
for _ in $(seq 1 30); do
  if curl -sf http://localhost:8420/health > /dev/null 2>&1; then
    break
  fi
  sleep 3
done
curl -s http://localhost:8420/health; echo

echo "==> Waiting ~75s for rules to accumulate sustained-breach incidents..."
sleep 75

echo "==> Containers Guardian currently sees:"
docker exec guardian-demo-service guardian containers

echo "==> Open incidents:"
docker exec guardian-demo-service guardian incidents list

echo "==> Full detail on the most severe incident:"
MOST_SEVERE=$(curl -s http://localhost:8420/api/v1/incidents | python3 -c '
import json, sys
items = json.load(sys.stdin)["items"]
order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
items.sort(key=lambda i: order.get(i["severity"], 9))
print(items[0]["incident_id"] if items else "")
')
if [ -n "$MOST_SEVERE" ]; then
  docker exec guardian-demo-service guardian incident "$MOST_SEVERE"
fi

echo "==> Remediation dry-run against the unhealthy demo container (monitoring-only by default):"
docker exec guardian-demo-service guardian remediation dry-run guardian-demo-unhealthy

echo "==> Prometheus-format metrics (first 15 lines):"
curl -s http://localhost:8420/metrics | grep '^guardian_' | head -15

echo "==> Tearing down the demo stack..."
$COMPOSE down -v

echo "==> Done. Your other containers were never touched."
