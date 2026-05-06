from pathlib import Path


def test_dockerfile_uses_ubuntu_base_not_node_base():
    root = Path(__file__).resolve().parents[1]
    text = (root / "Dockerfile").read_text(encoding="utf-8")
    assert text.startswith("FROM ubuntu:24.04")
    assert "FROM node:" not in text
    assert "FROM node@" not in text


def test_dockerfile_installs_node_22_from_ubuntu_base():
    root = Path(__file__).resolve().parents[1]
    text = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG NODE_MAJOR=22" in text
    assert "https://deb.nodesource.com/node_${NODE_MAJOR}.x" in text
    assert "apt-get install -y --no-install-recommends" in text
    assert "nodejs" in text
    assert 'node --version | grep -E "^v${NODE_MAJOR}\\\\."' in text


def test_dockerfile_vendors_opencode_plugin_for_workspace_tool_resolution():
    root = Path(__file__).resolve().parents[1]
    text = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "ENV EFP_OPENCODE_TOOL_DEPS_DIR=/opt/opencode-tool-deps" in text
    assert 'npm install -g "opencode-ai@${OPENCODE_VERSION}" "@opencode-ai/plugin@${OPENCODE_VERSION}"' in text
    assert "npm install" in text
    assert '--prefix "${EFP_OPENCODE_TOOL_DEPS_DIR}"' in text
    assert '"@opencode-ai/plugin@${OPENCODE_VERSION}"' in text
    assert 'test -f "${EFP_OPENCODE_TOOL_DEPS_DIR}/node_modules/@opencode-ai/plugin/package.json"' in text


def test_entrypoint_materializes_tool_deps_before_opencode_serve():
    root = Path(__file__).resolve().parents[1]
    text = (root / "entrypoint.sh").read_text(encoding="utf-8")
    assert "python -m efp_opencode_adapter.init_assets" in text
    assert "python -m efp_opencode_adapter.tool_deps" in text
    assert "opencode serve" in text
    assert text.index("python -m efp_opencode_adapter.init_assets") < text.index("python -m efp_opencode_adapter.tool_deps")
    assert text.index("python -m efp_opencode_adapter.tool_deps") < text.index("opencode serve")


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
