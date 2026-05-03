import json

from efp_opencode_adapter.tool_sync import sync_tools


def test_wrapper_rendering_integration_guard(tmp_path):
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    (tools_dir / "manifest.yaml").write_text("tools: []\n", encoding="utf-8")
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
wrapper = '''import { tool } from "@opencode-ai/plugin"\nimport {{ spawn }} from "node:child_process"\nexport default tool({{\n  execute: async (context) => {{\n    spawn("python3", ["-m", "efp_tools.runner", "--tools-dir", a.tools_dir, "--json-stdin"], {{}})\n    return {{ runtime_type: "opencode", session_id: context.sessionID }}\n  }}\n}})\n'''
Path(a.opencode_tools_dir).mkdir(parents=True, exist_ok=True)
(Path(a.opencode_tools_dir)/'efp_context_echo.ts').write_text(wrapper, encoding='utf-8')
Path(a.state_dir).mkdir(parents=True, exist_ok=True)
(Path(a.state_dir)/'tools-index.json').write_text(json.dumps({'generated_at':'now','tools':[{'opencode_name':'efp_context_echo'}]}), encoding='utf-8')
""",
        encoding="utf-8",
    )

    sync_tools(tools_dir, tmp_path / "workspace/.opencode/tools", tmp_path / "state")
    wrapper = (tmp_path / "workspace/.opencode/tools/efp_context_echo.ts").read_text(encoding="utf-8")

    assert 'import { tool } from "@opencode-ai/plugin"' in wrapper
    assert "export default tool({" in wrapper
    assert 'spawn("python3"' in wrapper
    assert '"--tools-dir"' in wrapper
    assert '"--json-stdin"' in wrapper
    assert 'runtime_type: "opencode"' in wrapper
    assert "session_id: context.sessionID" in wrapper
    assert "spawnSync" not in wrapper
