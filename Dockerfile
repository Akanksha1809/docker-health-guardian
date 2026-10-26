FROM python:3.12-slim AS base

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

COPY config ./config

# The container needs the Docker socket mounted at runtime. Running as a
# non-root user is defense-in-depth, not a sandbox: socket access itself is
# equivalent to root on the host regardless of the in-container user (see
# docs/SECURITY.md). DOCKER_GID must match the host's docker group so this
# user can open the mounted socket -- override at build time if it differs.
ARG DOCKER_GID=999
RUN groupadd -g ${DOCKER_GID} dockerhost \
    && useradd --create-home --uid 1000 --gid dockerhost guardian \
    && mkdir -p /app/data && chown -R guardian:dockerhost /app
USER guardian

EXPOSE 8420

ENTRYPOINT ["guardian"]
CMD ["start", "--config", "config/guardian.yaml"]
