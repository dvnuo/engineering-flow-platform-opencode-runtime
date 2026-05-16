FROM golang:1.24-bookworm AS atlassian-tools

ARG ATLASSIAN_TOOLS_REPO=https://github.com/dvnuo/engineering-flow-platform-tools.git
# Runtime smoke expects this tools ref to expose Jira issue.map-csv and issue.bulk-create schemas.
ARG ATLASSIAN_TOOLS_REF=master

RUN set -eux; \
  git clone --depth 1 --branch "${ATLASSIAN_TOOLS_REF}" "${ATLASSIAN_TOOLS_REPO}" /src

WORKDIR /src
RUN set -eux; \
  go build -o /out/jira ./cmd/jira; \
  go build -o /out/confluence ./cmd/confluence

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
  npm install -g "opencode-ai@${OPENCODE_VERSION}"; \
  actual="$(opencode --version | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"; \
  test "${actual}" = "${OPENCODE_VERSION}"

COPY --from=atlassian-tools /out/jira /usr/local/bin/jira
COPY --from=atlassian-tools /out/confluence /usr/local/bin/confluence
RUN set -eux; \
  chmod 0755 /usr/local/bin/jira /usr/local/bin/confluence; \
  jira version --json >/dev/null; \
  confluence version --json >/dev/null; \
  jira commands --json >/dev/null; \
  jira schema issue.map-csv --json >/dev/null; \
  jira schema issue.bulk-create --json >/dev/null

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
