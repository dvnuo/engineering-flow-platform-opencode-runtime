from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_entrypoint_does_not_bootstrap_custom_tool_dependencies():
    text = (ROOT / "entrypoint.sh").read_text(encoding="utf-8")
    assert "Ensuring OpenCode custom tool dependencies" not in text
    assert "efp_opencode_adapter.tool_deps" not in text
    assert "EFP_OPENCODE_TOOL_DEPS_DIR" not in text


def test_dockerfile_does_not_vendor_opencode_plugin_for_custom_tools():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "EFP_OPENCODE_TOOL_DEPS_DIR" not in text
    assert "/opt/opencode-tool-deps" not in text
    assert "--prefix" not in text or "opencode-tool-deps" not in text
    assert 'npm install -g "opencode-ai@${OPENCODE_VERSION}" "@opencode-ai/plugin@${OPENCODE_VERSION}"' not in text


def test_tool_deps_module_removed():
    assert not (ROOT / "efp_opencode_adapter" / "tool_deps.py").exists()
