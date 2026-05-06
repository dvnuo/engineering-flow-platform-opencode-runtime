import json
import textwrap
from pathlib import Path

from efp_opencode_adapter.tool_sync import sync_tools


def test_tool_sync_snapshot(tmp_path):
    tools = tmp_path / 'tools'
    (tools / 'adapters/opencode').mkdir(parents=True)
    (tools / 'python/efp_tools').mkdir(parents=True)
    (tools / 'manifest.yaml').write_text('name: x\n', encoding='utf-8')
    (tools / 'python/efp_tools/__init__.py').write_text('', encoding='utf-8')
    (tools / 'python/efp_tools/registry.py').write_text(textwrap.dedent('''
        class D:
            tool_id = "efp.tool.context.echo"
            opencode_name = "efp_context_echo"
            name = "context_echo"
            description = "Echo context"
            domain = "context"
            type = "adapter_action"
            runtime_compat = ["native", "opencode"]
            policy_tags = ["context", "read_only"]
            requires_identity_binding = False
            mutation = False
            risk_level = "low"
            input_schema = {"type": "object", "properties": {"message": {"type": "string"}}}
            output_schema = {"type": "object"}
            enabled = True

        class R:
            def list_descriptors(self, **_kwargs):
                return [D()]

        def load_registry(_tools_dir):
            return R()
    '''), encoding='utf-8')
    (tools / 'adapters/opencode/generate_tools.py').write_text(textwrap.dedent('''
        import argparse
        from pathlib import Path

        p = argparse.ArgumentParser()
        p.add_argument("--tools-dir")
        p.add_argument("--opencode-tools-dir")
        p.add_argument("--state-dir")
        a = p.parse_args()
        out = Path(a.opencode_tools_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / 'efp_context_echo.ts').write_text(
            'import { tool } from "@opencode-ai/plugin"\\n'
            'export default tool({\\n'
            'runtime_type: "opencode",\\n'
            'session_id: context.sessionID,\\n'
            'spawn("python3", ["-m", "efp_tools.runner", "--tools-dir", ".", "--json-stdin"])\\n'
            '})\\n'
        )
    '''), encoding='utf-8')
    opdir = tmp_path / 'out'
    st = tmp_path / 'state'
    idx = sync_tools(tools, opdir, st)
    idx.pop('generated_at', None)
    expected = json.loads(Path('tests/fixtures/expected_tools_index_snapshot.json').read_text())
    assert idx == expected
    content = (opdir / 'efp_context_echo.ts').read_text()
    for frag in ['import { tool } from "@opencode-ai/plugin"', 'export default tool({', 'spawn("python3"', '"-m", "efp_tools.runner"', '"--tools-dir"', '"--json-stdin"', 'runtime_type: "opencode"', 'session_id: context.sessionID']:
        assert frag in content
