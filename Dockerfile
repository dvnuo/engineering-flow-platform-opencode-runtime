FROM node:22-bookworm

ARG OPENCODE_VERSION=1.14.29
ENV OPENCODE_VERSION=${OPENCODE_VERSION}
ENV PYTHONUNBUFFERED=1
ENV PATH="/opt/venv/bin:${PATH}"

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    git curl ca-certificates jq ripgrep fd-find bash openssh-client tini \
  && rm -rf /var/lib/apt/lists/*

RUN npm install -g opencode-ai@${OPENCODE_VERSION} \
  && opencode --version

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
