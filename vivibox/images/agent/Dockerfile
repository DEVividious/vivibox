# Agent image: the toolchain an agent works with inside a task pod.
# Built locally by `vivibox image build` with your UID/GID, so files the agent writes
# in the task repo are yours on the host. There is no Docker daemon here, only the client;
# the daemon runs in the task's Sysbox sidecar.

ARG DOCKER_CLI_IMAGE=docker:29.8.1-cli
ARG NODE_IMAGE=node:24.21.0-trixie-slim
ARG MISE_IMAGE=jdxcode/mise:2026.9.11
ARG BASE_IMAGE=maven:3.9.16-amazoncorretto-21-debian

FROM ${DOCKER_CLI_IMAGE} AS docker-cli
FROM ${NODE_IMAGE} AS node
FROM ${MISE_IMAGE} AS mise

FROM ${BASE_IMAGE}

ARG OPENCODE_VERSION=1.18.31
ARG UID=1000
ARG GID=1000

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq less procps ripgrep tini unzip \
 && rm -rf /var/lib/apt/lists/*

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins /usr/local/libexec/docker/cli-plugins
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=mise /usr/local/bin/mise /usr/local/bin/mise

RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && ln -s ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack \
 && corepack enable \
 && npm install -g --no-fund --no-audit "opencode-ai@${OPENCODE_VERSION}" \
 && npm cache clean --force

# -o: tolerate an ID that already exists in the base image.
RUN groupadd -o -g "${GID}" agent \
 && useradd -o -u "${UID}" -g "${GID}" -d /config -M -s /bin/bash agent \
 && mkdir -p /config /cache/m2 /cache/gradle /cache/npm /cache/mise /cache/corepack \
 && chown -R agent:agent /config /cache

# HOME is /config, so Java's user.home, git, opencode and the docker client agree on one place.
# Caches live under /cache, mounted as volumes shared between tasks.
# MAVEN_CONFIG is cleared because the mvn script passes it on as command-line arguments.
# The file-lock settings let parallel builds share one local repository (needs Maven >= 3.9).
ENV HOME=/config \
    MAVEN_CONFIG= \
    MAVEN_ARGS="-Dmaven.repo.local=/cache/m2 -Daether.syncContext.named.factory=file-lock -Daether.syncContext.named.nameMapper=file-gav" \
    GRADLE_USER_HOME=/cache/gradle \
    npm_config_cache=/cache/npm \
    npm_config_update_notifier=false \
    COREPACK_HOME=/cache/corepack \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    MISE_DATA_DIR=/cache/mise \
    PATH=/cache/mise/shims:$PATH

USER agent
WORKDIR /config
ENTRYPOINT ["tini", "--"]
CMD ["bash"]
