import json

import pytest

from efp_opencode_adapter.tool_deps import ensure_tool_deps


def _write_vendored_plugin(vendored_dir, version="1.14.29"):
    package = vendored_dir / "node_modules" / "@opencode-ai" / "plugin" / "package.json"
    package.parent.mkdir(parents=True, exist_ok=True)
    package.write_text(json.dumps({"name": "@opencode-ai/plugin", "version": version}), encoding="utf-8")


def test_ensure_tool_deps_copies_plugin_and_writes_package_json(tmp_path):
    workspace = tmp_path / "workspace"
    vendored = tmp_path / "vendored"
    _write_vendored_plugin(vendored, "1.14.29")

    result = ensure_tool_deps(workspace_dir=workspace, vendored_dir=vendored)

    assert (workspace / ".opencode/node_modules/@opencode-ai/plugin/package.json").exists()
    package_json = json.loads((workspace / ".opencode/package.json").read_text(encoding="utf-8"))
    assert package_json["dependencies"]["@opencode-ai/plugin"] == "1.14.29"
    assert result["status"] == "ok"


def test_ensure_tool_deps_preserves_existing_package_json_fields(tmp_path):
    workspace = tmp_path / "workspace"
    vendored = tmp_path / "vendored"
    _write_vendored_plugin(vendored)
    config_dir = workspace / ".opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "package.json").write_text(
        json.dumps(
            {
                "name": "custom-name",
                "private": True,
                "dependencies": {"left-pad": "1.3.0"},
                "custom": {"keep": True},
            }
        ),
        encoding="utf-8",
    )

    ensure_tool_deps(workspace_dir=workspace, vendored_dir=vendored)
    payload = json.loads((config_dir / "package.json").read_text(encoding="utf-8"))

    assert payload["name"] == "custom-name"
    assert payload["custom"] == {"keep": True}
    assert payload["dependencies"]["left-pad"] == "1.3.0"
    assert payload["dependencies"]["@opencode-ai/plugin"] == "1.14.29"


def test_ensure_tool_deps_preserves_existing_node_modules_content(tmp_path):
    workspace = tmp_path / "workspace"
    vendored = tmp_path / "vendored"
    _write_vendored_plugin(vendored)
    existing = workspace / ".opencode/node_modules/some-existing/package.json"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text('{"name":"some-existing"}', encoding="utf-8")

    ensure_tool_deps(workspace_dir=workspace, vendored_dir=vendored)

    assert existing.exists()
    assert (workspace / ".opencode/node_modules/@opencode-ai/plugin/package.json").exists()


def test_ensure_tool_deps_fails_when_vendored_plugin_missing(tmp_path):
    with pytest.raises(RuntimeError, match="Missing vendored @opencode-ai/plugin"):
        ensure_tool_deps(workspace_dir=tmp_path / "workspace", vendored_dir=tmp_path / "vendored")


def test_ensure_tool_deps_rejects_invalid_existing_package_json(tmp_path):
    workspace = tmp_path / "workspace"
    vendored = tmp_path / "vendored"
    _write_vendored_plugin(vendored)
    package_json = workspace / ".opencode/package.json"
    package_json.parent.mkdir(parents=True, exist_ok=True)
    package_json.write_text("{invalid", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Invalid JSON"):
        ensure_tool_deps(workspace_dir=workspace, vendored_dir=vendored)
