from pathlib import Path


def _join(*parts: str) -> str:
    return "".join(parts)


def test_dockerfile_uses_ubuntu_base_not_node_base():
    root = Path(__file__).resolve().parents[1]
    text = (root / "Dockerfile").read_text(encoding="utf-8")
    assert text.startswith("FROM ubuntu:24.04")
    assert "FROM ubuntu:24.04" in text
    assert "FROM golang:" not in text
    assert "AS " + _join("atlassian", "-", "tools") not in text
    assert _join("ATLASSIAN", "_", "TOOLS_REPO") not in text
    assert _join("ATLASSIAN", "_", "TOOLS_REF") not in text
    assert "ARG OPENCODE_VERSION=1.14.39" in text
    assert "FROM node:" not in text
    assert "FROM node@" not in text


def test_dockerfile_uses_prebuilt_custom_tools_and_checks_schemas():
    root = Path(__file__).resolve().parents[1]
    text = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG CUSTOM_TOOLS_DIR=runtime-tools" in text
    assert "COPY ${CUSTOM_TOOLS_DIR}/jira /usr/local/bin/jira" in text
    assert "COPY ${CUSTOM_TOOLS_DIR}/confluence /usr/local/bin/confluence" in text
    assert "COPY ${CUSTOM_TOOLS_DIR}/mobile-auto /usr/local/bin/mobile-auto" in text
    assert "COPY ${CUSTOM_TOOLS_DIR}/BrowserStackLocal /usr/local/bin/BrowserStackLocal" in text
    assert "COPY --from=" + _join("atlassian", "-", "tools") not in text
    assert "jira version --json" in text
    assert "confluence version --json" in text
    assert "mobile-auto version --json" in text
    assert "jira commands --json" in text
    assert "mobile-auto commands --json" in text
    assert "jira schema issue.map-csv --json" in text
    assert "jira schema issue.bulk-create --json" in text
    assert "mobile-auto schema run.start --json" in text


def test_dockerfile_installs_node_22_from_ubuntu_base():
    root = Path(__file__).resolve().parents[1]
    text = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG NODE_MAJOR=22" in text
    assert "https://deb.nodesource.com/node_${NODE_MAJOR}.x" in text
    assert "apt-get install -y --no-install-recommends" in text
    assert "nodejs" in text
    assert 'node --version | grep -E "^v${NODE_MAJOR}\\\\."' in text


def test_dockerfile_installs_java_maven_runtime_tools():
    root = Path(__file__).resolve().parents[1]
    text = (root / "Dockerfile").read_text(encoding="utf-8")
    for token in [
        "zulu21-jdk",
        "ARG MAVEN_VERSION=3.9.16",
        "COPY ${MAVEN_SETTINGS_DIR}/settings.xml",
        "cat > /usr/local/bin/jdk",
        "cat > /usr/local/bin/mvn-jdk",
        "ENV JAVA_HOME=/opt/jdks/zulu21",
        "ENV JAVA21_HOME=/opt/jdks/zulu21",
        "ENV JDK21_HOME=/opt/jdks/zulu21",
    ]:
        assert token in text
    for token in [
        "zulu8-jdk",
        "zulu17-jdk",
        "zulu25-jdk",
        "ENV JAVA8_HOME=",
        "ENV JAVA17_HOME=",
        "ENV JAVA25_HOME=",
        "ENV JDK8_HOME=",
        "ENV JDK17_HOME=",
        "ENV JDK25_HOME=",
        "for v in 8 17 21 25",
        "mvn-jdk 8 -v",
        "mvn-jdk 17 -v",
        "mvn-jdk 25 -v",
    ]:
        assert token not in text


def test_dockerfile_installs_aws_cli_v2():
    root = Path(__file__).resolve().parents[1]
    text = (root / "Dockerfile").read_text(encoding="utf-8")
    for token in [
        "unzip",
        "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_CLI_ARCH}.zip",
        "/tmp/aws/install --bin-dir /usr/local/bin --install-dir /usr/local/aws-cli",
        "aws --version",
    ]:
        assert token in text


def test_dockerfile_installs_only_opencode_runtime_package():
    root = Path(__file__).resolve().parents[1]
    text = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "ENV EFP_OPENCODE_TOOL_DEPS_DIR=/opt/opencode-tool-deps" not in text
    assert 'npm install -g "opencode-ai@${OPENCODE_VERSION}"' in text
    assert '"@opencode-ai/plugin@${OPENCODE_VERSION}"' not in text
    assert 'test "${actual}" = "${OPENCODE_VERSION}"' in text


def test_dockerfile_preserves_opencode_snapshot_objects_during_git_gc():
    root = Path(__file__).resolve().parents[1]
    text = (root / "Dockerfile").read_text(encoding="utf-8")
    hook = (root / "scripts" / "opencode-snapshot-recent-objects").read_text(
        encoding="utf-8"
    )

    assert (
        "COPY scripts/opencode-snapshot-recent-objects "
        "/usr/local/bin/opencode-snapshot-recent-objects"
    ) in text
    assert (
        "git config --system gc.recentObjectsHook "
        "/usr/local/bin/opencode-snapshot-recent-objects"
    ) in text
    assert "bash -n /usr/local/bin/opencode-snapshot-recent-objects" in text
    smoke = (root / "scripts" / "smoke.sh").read_text(encoding="utf-8")
    assert "git config --system --get gc.recentObjectsHook" in smoke
    assert 'data_roots+=("${OPENCODE_DATA_DIR}")' in hook
    assert 'data_roots+=("${XDG_DATA_HOME%/}/opencode")' in hook
    assert '"${snapshot_root}"/*/*' in hook
    assert "git show-index" in hook


def test_entrypoint_bootstrap_order_for_managed_adapter_server():
    root = Path(__file__).resolve().parents[1]
    text = (root / "entrypoint.sh").read_text(encoding="utf-8")
    assert "python -m efp_opencode_adapter.init_assets" in text
    assert "python -m efp_opencode_adapter.server" in text
    assert "--manage-opencode" in text
    # Profile config arrives via the Secret env; there is no HTTP bootstrap step.
    assert "portal_runtime_context_bootstrap" not in text
    assert 'test -n "${EFP_PROFILE_CONFIG+x}"' in text
    assert text.index('test -n "${EFP_PROFILE_CONFIG+x}"') < text.index("python -m efp_opencode_adapter.init_assets")
    assert text.index("python -m efp_opencode_adapter.init_assets") < text.index("python -m efp_opencode_adapter.server")


def test_dockerfile_runs_as_root_with_root_state_dirs():
    root = Path(__file__).resolve().parents[1]
    text = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "USER root" in text
    assert "USER opencode" not in text
    assert "useradd --uid 10001" not in text
    assert "groupadd --gid 10001" not in text
    assert "opencode:opencode" not in text
    assert "/root/.local/share/opencode" in text
    assert "/root/.local/share/efp-compat" in text
    assert "/home/opencode" not in text


def test_managed_opencode_startup_checks_health_before_tool_registry():
    root = Path(__file__).resolve().parents[1]
    server_text = (root / "efp_opencode_adapter" / "server.py").read_text(encoding="utf-8")
    process_text = (root / "efp_opencode_adapter" / "opencode_process.py").read_text(encoding="utf-8")
    assert "app.on_startup.append(_managed_opencode_startup)" in server_text
    assert "await manager.start(projection.env, reason=\"startup\")" in server_text
    assert "await self.client.wait_until_ready" in process_text
