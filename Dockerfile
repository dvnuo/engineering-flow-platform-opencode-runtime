FROM ubuntu:24.04

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG OPENCODE_VERSION=1.14.39
ARG NODE_MAJOR=22
ARG DEBIAN_FRONTEND=noninteractive
ARG CUSTOM_TOOLS_DIR=runtime-tools
ARG MAVEN_VERSION=3.9.16
ARG MAVEN_SETTINGS_DIR=runtime-maven

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
  curl -fsSL https://repos.azul.com/azul-repo.key \
    | gpg --batch --yes --dearmor -o /etc/apt/keyrings/azul.gpg; \
  chmod a+r /etc/apt/keyrings/azul.gpg; \
  echo "deb [signed-by=/etc/apt/keyrings/azul.gpg] https://repos.azul.com/zulu/deb stable main" \
    > /etc/apt/sources.list.d/zulu.list; \
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
    gh \
    zulu8-jdk \
    zulu17-jdk \
    zulu21-jdk \
    zulu25-jdk; \
  mkdir -p /opt/jdks; \
  for v in 8 17 21 25; do \
    home="$(find /usr/lib/jvm -maxdepth 1 -type d -name "zulu${v}-ca-*" | sort | head -1)"; \
    if [[ -z "$home" ]]; then home="$(find /usr/lib/jvm -maxdepth 1 -type d -name "zulu${v}*" | sort | head -1)"; fi; \
    test -n "$home"; \
    test -x "$home/bin/java"; \
    ln -sfn "$home" "/opt/jdks/zulu${v}"; \
  done; \
  node --version | grep -E "^v${NODE_MAJOR}\\."; \
  npm --version; \
  git --version; \
  gh --version; \
  test "$(npm root -g)" = "/usr/local/lib/node_modules"; \
  rm -rf /var/lib/apt/lists/*

ENV JAVA8_HOME=/opt/jdks/zulu8
ENV JAVA17_HOME=/opt/jdks/zulu17
ENV JAVA21_HOME=/opt/jdks/zulu21
ENV JAVA25_HOME=/opt/jdks/zulu25
ENV JDK8_HOME=/opt/jdks/zulu8
ENV JDK17_HOME=/opt/jdks/zulu17
ENV JDK21_HOME=/opt/jdks/zulu21
ENV JDK25_HOME=/opt/jdks/zulu25
ENV JAVA_HOME=/opt/jdks/zulu21

RUN set -eux; \
  curl -fsSL "https://dlcdn.apache.org/maven/maven-3/${MAVEN_VERSION}/binaries/apache-maven-${MAVEN_VERSION}-bin.tar.gz" \
    -o /tmp/apache-maven.tar.gz; \
  mkdir -p /opt; \
  tar -xzf /tmp/apache-maven.tar.gz -C /opt; \
  ln -sfn "/opt/apache-maven-${MAVEN_VERSION}" /opt/maven; \
  ln -sfn /opt/maven/bin/mvn /usr/local/bin/mvn; \
  rm -f /tmp/apache-maven.tar.gz; \
  /opt/maven/bin/mvn -v

ENV MAVEN_HOME=/opt/maven
ENV M2_HOME=/opt/maven
ENV MAVEN_CONFIG=/root/.m2
ENV MAVEN_SETTINGS_PATH=/root/.m2/settings.xml
ENV PATH="/opt/jdks/zulu21/bin:/opt/maven/bin:/opt/venv/bin:/usr/local/bin:${PATH}"

RUN <<'EOF'
set -eux
cat > /usr/local/bin/jdk <<'SCRIPT'
#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage:
  jdk list
  jdk current
  jdk <8|17|21|25> <command> [args...]
USAGE
}

case "${1:-}" in
  list)
    for version in 8 17 21 25; do
      printf "%s\t%s\n" "${version}" "/opt/jdks/zulu${version}"
    done
    exit 0
    ;;
  current)
    printf "JAVA_HOME=%s\n" "${JAVA_HOME:-/opt/jdks/zulu21}"
    java -version
    exit 0
    ;;
  8|17|21|25)
    version="$1"
    shift
    ;;
  *)
    usage
    exit 2
    ;;
esac

home="/opt/jdks/zulu${version}"
test -x "${home}/bin/java"
export JAVA_HOME="${home}"
export PATH="${JAVA_HOME}/bin:/opt/maven/bin:${PATH}"

if [[ "$#" -eq 0 ]]; then
  exec java -version
fi

exec "$@"
SCRIPT

cat > /usr/local/bin/mvn-jdk <<'SCRIPT'
#!/usr/bin/env bash
set -euo pipefail

version="21"
if [[ "$#" -gt 0 && "$1" =~ ^(8|17|21|25)$ ]]; then
  version="$1"
  shift
fi

home="/opt/jdks/zulu${version}"
test -x "${home}/bin/java"
export JAVA_HOME="${home}"
export PATH="${JAVA_HOME}/bin:/opt/maven/bin:${PATH}"
exec mvn "$@"
SCRIPT

chmod 0755 /usr/local/bin/jdk /usr/local/bin/mvn-jdk
EOF

COPY ${MAVEN_SETTINGS_DIR}/settings.xml /tmp/maven-settings/settings.xml
RUN <<'EOF'
set -eux
install -d -m 0700 /root/.m2
install -d -m 0700 /root/.local/share/efp-compat/maven
python3 - <<'PY'
import xml.etree.ElementTree as ET
ET.parse("/tmp/maven-settings/settings.xml")
PY
install -m 0600 /tmp/maven-settings/settings.xml /root/.m2/settings.xml
ln -sfn /root/.m2/settings.xml /root/.local/share/efp-compat/maven/settings.xml
cat > /root/.m2/toolchains.xml <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<toolchains xmlns="http://maven.apache.org/TOOLCHAINS/1.1.0"
            xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
            xsi:schemaLocation="http://maven.apache.org/TOOLCHAINS/1.1.0 https://maven.apache.org/xsd/toolchains-1.1.0.xsd">
  <toolchain>
    <type>jdk</type>
    <provides><version>8</version><vendor>azul</vendor></provides>
    <configuration><jdkHome>/opt/jdks/zulu8</jdkHome></configuration>
  </toolchain>
  <toolchain>
    <type>jdk</type>
    <provides><version>17</version><vendor>azul</vendor></provides>
    <configuration><jdkHome>/opt/jdks/zulu17</jdkHome></configuration>
  </toolchain>
  <toolchain>
    <type>jdk</type>
    <provides><version>21</version><vendor>azul</vendor></provides>
    <configuration><jdkHome>/opt/jdks/zulu21</jdkHome></configuration>
  </toolchain>
  <toolchain>
    <type>jdk</type>
    <provides><version>25</version><vendor>azul</vendor></provides>
    <configuration><jdkHome>/opt/jdks/zulu25</jdkHome></configuration>
  </toolchain>
</toolchains>
XML
chmod 0600 /root/.m2/toolchains.xml
ln -sfn /root/.m2/toolchains.xml /root/.local/share/efp-compat/maven/toolchains.xml
test -f /root/.m2/settings.xml
test -f /root/.m2/toolchains.xml
EOF

RUN set -eux; \
  java -version; \
  javac -version; \
  mvn -v; \
  jdk list; \
  mvn-jdk 8 -v; \
  mvn-jdk 17 -v; \
  mvn-jdk 21 -v; \
  mvn-jdk 25 -v

RUN set -eux; \
  npm install -g "opencode-ai@${OPENCODE_VERSION}"; \
  actual="$(opencode --version | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"; \
  test "${actual}" = "${OPENCODE_VERSION}"

COPY ${CUSTOM_TOOLS_DIR}/jira /usr/local/bin/jira
COPY ${CUSTOM_TOOLS_DIR}/confluence /usr/local/bin/confluence
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
