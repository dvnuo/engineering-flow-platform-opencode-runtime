import json
import os

import pytest

from efp_opencode_adapter.tool_sync import sync_tools


def test_sync_tools_missing_repo_writes_empty_index(tmp_path):
    tools_dir = tmp_path / "missing-tools"
    opencode_tools_dir = tmp_path / "workspace/.opencode/tools"
    state_dir = tmp_path / "state"

    with pytest.warns(UserWarning, match="tools directory does not exist"):
        index = sync_tools(tools_dir, opencode_tools_dir, state_dir)

    payload = json.loads((state_dir / "tools-index.json").read_text(encoding="utf-8"))
    assert payload["tools"] == []
    assert index["tools"] == []
    assert payload["warnings"]


def test_sync_tools_keeps_legacy_generator_contract(tmp_path):
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    (tools_dir / "manifest.yaml").write_text("tools: []\n", encoding="utf-8")
    (tools_dir / "python" / "efp_tools").mkdir(parents=True, exist_ok=True)
    (tools_dir / "python" / "efp_tools" / "__init__.py").write_text("", encoding="utf-8")
    gen = tools_dir / "adapters" / "opencode" / "generate_tools.py"
    gen.parent.mkdir(parents=True, exist_ok=True)
    gen.write_text(
        """
import argparse, json
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('--tools-dir', required=True)
p.add_argument('--opencode-tools-dir', required=True)
p.add_argument('--state-dir', required=True)
a=p.parse_args()
Path(a.opencode_tools_dir).mkdir(parents=True, exist_ok=True)
Path(a.state_dir).mkdir(parents=True, exist_ok=True)
(Path(a.opencode_tools_dir)/'efp_context_echo.ts').write_text('// wrapper', encoding='utf-8')
(Path(a.state_dir)/'tools-index.json').write_text(json.dumps({'generated_at':'now','tools':[{'opencode_name':'efp_context_echo'}]}), encoding='utf-8')
""",
        encoding="utf-8",
    )

    index = sync_tools(tools_dir, tmp_path / "workspace/.opencode/tools", tmp_path / "state")

    assert (tmp_path / "workspace/.opencode/tools/efp_context_echo.ts").exists()
    assert index["tools"][0]["opencode_name"] == "efp_context_echo"


def test_sync_tools_generator_failure_redacts_and_truncates(tmp_path, monkeypatch):
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    (tools_dir / "manifest.yaml").write_text("tools: []\n", encoding="utf-8")
    gen = tools_dir / "adapters" / "opencode" / "generate_tools.py"
    gen.parent.mkdir(parents=True, exist_ok=True)
    secret = "super-secret-token-value"
    monkeypatch.setenv("SERVICE_TOKEN", secret)
    long_payload = "X" * 5000
    gen.write_text(
        f"import sys; print('{secret}'); print('{long_payload}'); sys.exit(2)",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError) as exc:
        sync_tools(tools_dir, tmp_path / "workspace/.opencode/tools", tmp_path / "state")

    message = str(exc.value)
    assert secret not in message
    assert "***REDACTED***" in message
    assert len(message) < 4600


def test_sync_tools_manifest_without_generator_fails(tmp_path):
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    (tools_dir / "manifest.yaml").write_text("tools: []\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="tools generator missing"):
        sync_tools(tools_dir, tmp_path / "workspace/.opencode/tools", tmp_path / "state")


def test_sync_tools_supports_latest_output_dir_generator_and_writes_rich_index(tmp_path):
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    (tools_dir / "manifest.yaml").write_text("tools: []\n", encoding="utf-8")
    gen = tools_dir / "adapters" / "opencode" / "generate_tools.py"
    gen.parent.mkdir(parents=True, exist_ok=True)
    gen.write_text("""
import argparse
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--tools-dir', required=True);p.add_argument('--output-dir', required=True);a=p.parse_args()
Path(a.output_dir).mkdir(parents=True, exist_ok=True)
(Path(a.output_dir)/'efp_context_echo.ts').write_text('// wrapper', encoding='utf-8')
""", encoding='utf-8')
    pkg = tools_dir / 'python' / 'efp_tools'
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / '__init__.py').write_text('', encoding='utf-8')
    (pkg / 'registry.py').write_text("""
class Descriptor:
    tool_id='efp.tool.context.echo';name='context_echo';opencode_name='efp_context_echo';description='Echo context';domain='context';type='adapter_action';runtime_compat=['native','opencode'];policy_tags=['context','read_only'];requires_identity_binding=False;mutation=False;risk_level='low';input_schema={'type':'object','properties':{'message':{'type':'string'}}};output_schema={'type':'object'};enabled=True
class Registry:
    def list_descriptors(self, runtime_type=None, enabled_only=True, model_facing_only=True):
        return [Descriptor()]
def load_registry(tools_dir):
    return Registry()
""", encoding='utf-8')
    out = tmp_path / 'workspace/.opencode/tools'
    state = tmp_path / 'state'
    index = sync_tools(tools_dir, out, state)
    assert (out / 'efp_context_echo.ts').exists()
    assert (state / 'tools-index.json').exists()
    assert index['tools'][0]['opencode_name'] == 'efp_context_echo'
    assert index['tools'][0]['capability_id'] == 'efp.tool.context.echo'
    assert index['tools'][0]['policy_tags'] == ['context', 'read_only']
    assert index['tools'][0]['input_schema']['type'] == 'object'
    assert index['source'] == 'efp_tools.registry'
