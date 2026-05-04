FROM ubuntu:24.04

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG OPENCODE_VERSION=1.14.29
ARG NODE_MAJOR=22
ARG DEBIAN_FRONTEND=noninteractive

ENV OPENCODE_VERSION=${OPENCODE_VERSION}
ENV PYTHONUNBUFFERED=1
ENV PATH="/opt/venv/bin:/usr/local/bin:${PATH}"
ENV NODE_PATH=/usr/local/lib/node_modules
ENV NPM_CONFIG_PREFIX=/usr/local

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
    tini; \
  node --version | grep -E "^v${NODE_MAJOR}\\."; \
  npm --version; \
  test "$(npm root -g)" = "/usr/local/lib/node_modules"; \
  rm -rf /var/lib/apt/lists/*

RUN set -eux; \
  npm install -g "opencode-ai@${OPENCODE_VERSION}" "@opencode-ai/plugin@${OPENCODE_VERSION}"; \
  opencode --version | grep -F "${OPENCODE_VERSION}"

RUN groupadd --gid 10001 opencode \
  && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash opencode

WORKDIR /app/runtime
COPY pyproject.toml README.md package*.json ./
COPY efp_opencode_adapter ./efp_opencode_adapter
RUN python3 -m venv /opt/venv \
  && /opt/venv/bin/pip install --upgrade pip \
  && /opt/venv/bin/pip install -e .

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY scripts/smoke.sh /app/runtime/scripts/smoke.sh

RUN chmod +x /usr/local/bin/entrypoint.sh /app/runtime/scripts/smoke.sh \
  && mkdir -p \
    /workspace/.opencode/skills \
    /workspace/.opencode/tools \
    /workspace/.opencode/agents \
    /app/skills \
    /app/tools \
    /home/opencode/.local/share/opencode \
    /home/opencode/.local/share/efp-compat \
  && chown -R opencode:opencode \
    /workspace \
    /app/skills \
    /app/tools \
    /home/opencode

WORKDIR /workspace
USER opencode
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
