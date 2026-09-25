# Agent image: the toolchain an agent works with inside a task pod.
# Built locally by `vivibox image build` with your UID/GID, so files the agent writes
# in the task repo are yours on the host. There is no Docker daemon here, only the client;
# the daemon runs in the task's Sysbox sidecar.

ARG DOCKER_CLI_IMAGE=docker:29.8.1-cli
ARG NODE_IMAGE=node:24.21.0-trixie-slim
ARG MISE_IMAGE=jdxcode/mise:2026.9.11
ARG BASE_IMAGE=maven:3.9.16-amazoncorretto-21-debian
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.8

FROM ${DOCKER_CLI_IMAGE} AS docker-cli
FROM ${NODE_IMAGE} AS node
FROM ${MISE_IMAGE} AS mise
FROM ${UV_IMAGE} AS uv

FROM ${BASE_IMAGE}

ARG OPENCODE_VERSION=1.18.31
ARG CLAUDE_CODE_VERSION=2.1.278
ARG SERENA_VERSION=v1.7.0
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
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && npm cache clean --force

# Serena, the MCP server vivibox offers itself (off until you turn it on): installed here with its
# own Python, outside /cache, so a project's mise versions cannot break it and it needs no download
# when a task starts. It reads the task's repository through language servers it fetches per language.
COPY --from=uv /uv /usr/local/bin/uv
RUN UV_PYTHON_INSTALL_DIR=/opt/python UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin \
    uv tool install --python 3.13 --no-cache "git+https://github.com/oraios/serena@${SERENA_VERSION}" \
 && serena --help >/dev/null

# Default toolchains. /etc/mise/config.toml is mise's system-wide config, so it applies in the gate
# container too, whose /config is a fresh tmpfs that loses anything mise writes to HOME. Without it
# the shims are on PATH and fail with "no version set", which reads as "this language is missing"
# and quietly pushed every agent towards node. A project that wants other versions, or another
# language entirely, puts its own mise.toml in the repository: that file wins over this one, and
# the user approves it as a build file before it reaches their checkout.
RUN mkdir -p /etc/mise \
 && printf '[tools]\npython = "3.13"\nuv = "0.12"\njava = "corretto-21"\n' > /etc/mise/config.toml

# -o: tolerate an ID that already exists in the base image.
RUN groupadd -o -g "${GID}" agent \
 && useradd -o -u "${UID}" -g "${GID}" -d /config -M -s /bin/bash agent \
 && mkdir -p /config /cache/m2 /cache/gradle /cache/npm /cache/mise /cache/corepack /cache/build-cache \
    /cache/m2/installed \
 && chown -R agent:agent /config /cache

# HOME is /config, so Java's user.home, git, opencode and the docker client agree on one place.
# Caches live under /cache, mounted as volumes shared between tasks.
# MAVEN_CONFIG is cleared because the mvn script passes it on as command-line arguments.
# The local repository is set twice: MAVEN_ARGS is read by Maven 3.9 and later, MAVEN_OPTS by every
# Maven the mvnw wrapper of a project may fetch, older ones included; a build that fell back to
# $HOME/.m2 left the gate, whose home is fresh, without what the writer had built.
# The file-lock settings let parallel builds share one local repository (needs Maven >= 3.9).
# The split puts what Maven downloads under cached/, shared, and what a build installs under
# installed/, where each task mounts a volume of its own: one task's unfinished modules never reach
# another's build. A fresh volume takes its owner from the image, hence the directory above.
ENV HOME=/config \
    MAVEN_CONFIG= \
    MAVEN_ARGS="-Dmaven.repo.local=/cache/m2 -Daether.syncContext.named.factory=file-lock -Daether.syncContext.named.nameMapper=file-gav -Daether.enhancedLocalRepository.split=true" \
    MAVEN_OPTS="-Dmaven.repo.local=/cache/m2" \
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
