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


def test_dockerfile_preserves_global_opencode_plugin_resolution():
    root = Path(__file__).resolve().parents[1]
    text = (root / "Dockerfile").read_text(encoding="utf-8")

    assert "ENV NPM_CONFIG_PREFIX=/usr/local" in text
    assert "ENV NODE_PATH=/usr/local/lib/node_modules" in text
    assert 'test "$(npm root -g)" = "/usr/local/lib/node_modules"' in text
    assert 'npm install -g "opencode-ai@${OPENCODE_VERSION}" "@opencode-ai/plugin@${OPENCODE_VERSION}"' in text
    assert 'opencode --version | grep -F "${OPENCODE_VERSION}"' in text
