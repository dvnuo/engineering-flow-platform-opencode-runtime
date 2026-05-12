FROM ubuntu:24.04

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG OPENCODE_VERSION=1.14.39
ARG NODE_MAJOR=22
ARG DEBIAN_FRONTEND=noninteractive

ENV OPENCODE_VERSION=${OPENCODE_VERSION}
ENV PYTHONUNBUFFERED=1
ENV PATH="/opt/venv/bin:/usr/local/bin:${PATH}"
ENV NODE_PATH=/usr/local/lib/node_modules
ENV NPM_CONFIG_PREFIX=/usr/local
ENV HOME=/root
ENV EFP_OPENCODE_TOOL_DEPS_DIR=/opt/opencode-tool-deps

RUN set -eux; \
  apt-get update; \
  apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg; \
  mkdir -p /etc/apt/keyrings; \
  curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
    | gpg --batch --yes --dearmor -o /etc/apt/keyrings/nodesource.gpg; \
  chmod a+r /etc/apt/keyrings/nodesource.gpg; \
  echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
    > /etc/apt/sources.list.d/nodesource.list; \
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | gpg --batch --yes --dearmor -o /etc/apt/keyrings/githubcli-archive-keyring.gpg; \
  chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg; \
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list; \
  apt-get update; \
  apt-get install -y --no-install-recommends \
    nodejs \
    python3 \
    python3-venv \
    python3-pip \
    git \
    jq \
    ripgrep \
    fd-find \
    bash \
    openssh-client \
    tini \
    gh; \
  node --version | grep -E "^v${NODE_MAJOR}\\."; \
  npm --version; \
  git --version; \
  gh --version; \
  test "$(npm root -g)" = "/usr/local/lib/node_modules"; \
  rm -rf /var/lib/apt/lists/*

RUN set -eux; \
  npm install -g "opencode-ai@${OPENCODE_VERSION}" "@opencode-ai/plugin@${OPENCODE_VERSION}"; \
  mkdir -p "${EFP_OPENCODE_TOOL_DEPS_DIR}"; \
  npm install \
    --prefix "${EFP_OPENCODE_TOOL_DEPS_DIR}" \
    --omit=dev \
    --ignore-scripts \
    --no-audit \
    --no-fund \
    "@opencode-ai/plugin@${OPENCODE_VERSION}"; \
  test -f "${EFP_OPENCODE_TOOL_DEPS_DIR}/node_modules/@opencode-ai/plugin/package.json"; \
  actual="$(opencode --version | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"; \
  test "${actual}" = "${OPENCODE_VERSION}"; \
  node -e '\
const fs = require("fs")\
const path = process.env.EFP_OPENCODE_TOOL_DEPS_DIR + "/node_modules/@opencode-ai/plugin/package.json"\
const actual = JSON.parse(fs.readFileSync(path, "utf8")).version\
if (actual !== process.env.OPENCODE_VERSION) {\
  throw new Error(`vendored @opencode-ai/plugin version ${actual} != OPENCODE_VERSION ${process.env.OPENCODE_VERSION}`)\
}\
'

WORKDIR /app/runtime
COPY pyproject.toml README.md package*.json ./
COPY efp_opencode_adapter ./efp_opencode_adapter
COPY workspace ./workspace
RUN python3 -m venv /opt/venv \
  && /opt/venv/bin/pip install --upgrade pip \
  && /opt/venv/bin/pip install -e .

COPY entrypoint.sh /tmp/entrypoint.sh
COPY scripts/smoke.sh /app/runtime/scripts/smoke.sh

RUN sed -i 's/\r$//' /tmp/entrypoint.sh /app/runtime/scripts/smoke.sh \
  && install -o root -g root -m 0755 /tmp/entrypoint.sh /usr/local/bin/entrypoint.sh \
  && chmod 0755 /app/runtime/scripts/smoke.sh \
  && rm -f /tmp/entrypoint.sh \
  && mkdir -p \
    /workspace/.opencode/skills \
    /workspace/.opencode/agents \
    /app/skills \
    /root/.local/share/opencode \
    /root/.local/share/efp-compat

WORKDIR /workspace
USER root
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
