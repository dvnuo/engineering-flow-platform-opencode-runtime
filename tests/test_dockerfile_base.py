from pathlib import Path


def test_dockerfile_uses_ubuntu_base_not_node_base():
    root = Path(__file__).resolve().parents[1]
    text = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM ubuntu:24.04" in text
    assert "FROM golang:" in text
    assert "AS atlassian-tools" in text
    assert "ARG OPENCODE_VERSION=1.14.39" in text
    assert "FROM node:" not in text
    assert "FROM node@" not in text


def test_dockerfile_keeps_atlassian_tools_ref_configurable_and_checks_schemas():
    root = Path(__file__).resolve().parents[1]
    text = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG ATLASSIAN_TOOLS_REPO=" in text
    assert "ARG ATLASSIAN_TOOLS_REF=master" in text
    assert "jira version --json" in text
    assert "confluence version --json" in text
    assert "jira commands --json" in text
    assert "jira schema issue.map-csv --json" in text
    assert "jira schema issue.bulk-create --json" in text


def test_dockerfile_installs_node_22_from_ubuntu_base():
    root = Path(__file__).resolve().parents[1]
    text = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG NODE_MAJOR=22" in text
    assert "https://deb.nodesource.com/node_${NODE_MAJOR}.x" in text
    assert "apt-get install -y --no-install-recommends" in text
    assert "nodejs" in text
    assert 'node --version | grep -E "^v${NODE_MAJOR}\\\\."' in text


def test_dockerfile_installs_only_opencode_runtime_package():
    root = Path(__file__).resolve().parents[1]
    text = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "ENV EFP_OPENCODE_TOOL_DEPS_DIR=/opt/opencode-tool-deps" not in text
    assert 'npm install -g "opencode-ai@${OPENCODE_VERSION}"' in text
    assert '"@opencode-ai/plugin@${OPENCODE_VERSION}"' not in text
    assert 'test "${actual}" = "${OPENCODE_VERSION}"' in text


def test_entrypoint_bootstrap_order_for_managed_adapter_server():
    root = Path(__file__).resolve().parents[1]
    text = (root / "entrypoint.sh").read_text(encoding="utf-8")
    assert "python -m efp_opencode_adapter.init_assets" in text
    assert "python -m efp_opencode_adapter.portal_runtime_context_bootstrap" in text
    assert "python -m efp_opencode_adapter.server" in text
    assert "--manage-opencode" in text
    assert text.index("python -m efp_opencode_adapter.init_assets") < text.index("python -m efp_opencode_adapter.portal_runtime_context_bootstrap")
    assert text.index("python -m efp_opencode_adapter.portal_runtime_context_bootstrap") < text.index("python -m efp_opencode_adapter.server")


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
    assert "await manager.start(env, reason=\"startup\")" in server_text
    assert "await self.client.wait_until_ready" in process_text
