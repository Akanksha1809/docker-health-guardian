.PHONY: venv install test lint fmt run-cli compose-up compose-down compose-observability detect-docker-gid demo-up demo-down

VENV := .venv/bin

venv:
	python3 -m venv .venv
	$(VENV)/pip install -q --upgrade pip

install: venv
	$(VENV)/pip install -q -e ".[dev]"

test:
	$(VENV)/python -m pytest

test-cov:
	$(VENV)/python -m pytest --cov --cov-report=term-missing

lint:
	$(VENV)/ruff check src tests

fmt:
	$(VENV)/ruff check --fix src tests

run-cli:
	$(VENV)/python -m docker_health_guardian.cli.main $(ARGS)

# Prints the GID that owns /var/run/docker.sock as seen *inside* a Linux
# container on this host (not the same as `stat` on macOS -- see
# docs/SECURITY.md). Use: `export DOCKER_GID=$$(make -s detect-docker-gid)`
detect-docker-gid:
	@docker run --rm -v /var/run/docker.sock:/var/run/docker.sock busybox:1.36 \
		stat -c '%g' /var/run/docker.sock

demo-up:
	DOCKER_GID=$$(docker run --rm -v /var/run/docker.sock:/var/run/docker.sock busybox:1.36 stat -c '%g' /var/run/docker.sock) \
		docker-compose -f deploy/docker-compose.yml up -d --build

demo-down:
	docker-compose -f deploy/docker-compose.yml down

demo-observability-up:
	DOCKER_GID=$$(docker run --rm -v /var/run/docker.sock:/var/run/docker.sock busybox:1.36 stat -c '%g' /var/run/docker.sock) \
		docker-compose -f deploy/docker-compose.yml --profile observability up -d --build

demo-observability-down:
	docker-compose -f deploy/docker-compose.yml --profile observability down
