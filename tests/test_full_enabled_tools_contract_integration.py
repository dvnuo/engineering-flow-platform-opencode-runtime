import json
import os
import tempfile
from pathlib import Path

import pytest

from efp_opencode_adapter.init_assets import init_assets
from efp_opencode_adapter.permission_generator import build_permission
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.skill_invocation import evaluate_skill_invocation, parse_slash_invocation
from efp_opencode_adapter.skill_sync import sync_skills
from efp_opencode_adapter.tool_sync import sync_tools


def _write_generator_repo(root: Path, *, tools_payload: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.yaml").write_text("tools: []\n", encoding="utf-8")
    gen = root / "adapters" / "opencode" / "generate_tools.py"
    gen.parent.mkdir(parents=True, exist_ok=True)
    gen.write_text(
        """
import argparse, json
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--tools-dir', required=True); p.add_argument('--opencode-tools-dir', required=True); p.add_argument('--state-dir', required=True)
a=p.parse_args()
Path(a.opencode_tools_dir).mkdir(parents=True, exist_ok=True)
for tool in ['efp_git_clone.ts','efp_run_command.ts','efp_github_create_pull_request.ts']:
    (Path(a.opencode_tools_dir)/tool).write_text('// wrapper', encoding='utf-8')
Path(a.state_dir).mkdir(parents=True, exist_ok=True)
(Path(a.state_dir)/'tools-index.json').write_text(json.dumps(""" + repr(tools_payload) + """), encoding='utf-8')
""",
        encoding="utf-8",
    )


def _sample_tools_payload():
    return {
        "generated_at": "now",
        "tools": [
            {
                "capability_id": "efp.tool.git.git_clone",
                "tool_id": "efp.tool.git.git_clone",
                "legacy_name": "git_clone",
                "opencode_name": "efp_git_clone",
                "name": "efp_git_clone",
                "domain": "git",
                "policy_tags": ["mutation", "execute"],
                "mutation": True,
                "permission_default": "ask",
                "dry_run_supported": False,
                "audit_event": "git.clone",
                "side_effects": ["filesystem_write"],
                "idempotency_key_fields": ["repo_url", "target_path"],
                "governance_reviewed": True,
                "input_schema": {"type": "object"},
                "enabled": True,
                "runtime_compat": ["opencode"],
            },
            {
                "capability_id": "efp.tool.shell.run_command",
                "tool_id": "efp.tool.shell.run_command",
                "legacy_name": "run_command",
                "opencode_name": "efp_run_command",
                "name": "efp_run_command",
                "domain": "shell",
                "policy_tags": ["mutation", "execute"],
                "mutation": True,
                "permission_default": "ask",
                "dry_run_supported": True,
                "audit_event": "shell.run_command",
                "side_effects": ["shell_exec"],
                "idempotency_key_fields": ["command"],
                "governance_reviewed": True,
                "input_schema": {"type": "object"},
                "enabled": True,
                "runtime_compat": ["opencode"],
            },
            {
                "capability_id": "efp.tool.github.github_create_pull_request",
                "tool_id": "efp.tool.github.github_create_pull_request",
                "legacy_name": "github_create_pull_request",
                "opencode_name": "efp_github_create_pull_request",
                "name": "efp_github_create_pull_request",
                "domain": "github",
                "policy_tags": ["github", "mutation", "write", "requires_approval"],
                "mutation": True,
                "permission_default": "ask",
                "dry_run_supported": True,
                "audit_event": "github.create_pull_request",
                "side_effects": ["remote_write"],
                "idempotency_key_fields": ["repo", "base", "head", "title"],
                "governance_reviewed": True,
                "input_schema": {"type": "object"},
                "enabled": True,
                "runtime_compat": ["opencode"],
            },
            {
                "capability_id": "efp.tool.github.github_get_default_branch",
                "tool_id": "efp.tool.github.github_get_default_branch",
                "legacy_name": "github_get_default_branch",
                "opencode_name": "efp_github_get_default_branch",
                "name": "efp_github_get_default_branch",
                "domain": "github",
                "policy_tags": ["github", "read_only"],
                "mutation": False,
                "permission_default": "allow",
                "dry_run_supported": True,
                "audit_event": "github.get_default_branch",
                "side_effects": [],
                "idempotency_key_fields": ["repo"],
                "governance_reviewed": True,
                "input_schema": {"type": "object"},
                "enabled": True,
                "runtime_compat": ["opencode"],
            },
        ],
    }


def test_full_enabled_tools_contract_generator_priority(tmp_path):
    tools_dir = tmp_path / "tools"
    payload = _sample_tools_payload()
    _write_generator_repo(tools_dir, tools_payload=payload)

    out = sync_tools(tools_dir, tmp_path / "workspace/.opencode/tools", tmp_path / "state")
    assert {t["legacy_name"] for t in out["tools"]} >= {"git_clone", "run_command", "github_create_pull_request"}
    for name in ["efp_git_clone", "efp_run_command", "efp_github_create_pull_request"]:
        tool = next(t for t in out["tools"] if t["opencode_name"] == name)
        for key in ["permission_default", "dry_run_supported", "audit_event", "side_effects", "idempotency_key_fields", "governance_reviewed", "policy_tags", "mutation", "input_schema"]:
            assert key in tool
    state_payload = json.loads((tmp_path / "state" / "tools-index.json").read_text(encoding="utf-8"))
    assert state_payload["tools"][0]["opencode_name"] == payload["tools"][0]["opencode_name"]


def test_full_enabled_tools_contract_permission_rules():
    idx = _sample_tools_payload()
    perm = build_permission({}, tools_index=idx, permission_mode="workspace_full_access")
    assert perm["efp_github_create_pull_request"] == "ask"
    assert perm["efp_git_clone"] == "ask"
    assert perm["efp_github_get_default_branch"] == "allow"

    perm_explicit = build_permission({"llm": {"tools": {"allow": ["efp_github_create_pull_request"]}}}, tools_index=idx, permission_mode="workspace_full_access")
    assert perm_explicit["efp_github_create_pull_request"] == "allow"

    perm_denied = build_permission({"denied_actions": ["efp_github_create_pull_request"]}, tools_index=idx, permission_mode="workspace_full_access")
    assert perm_denied["efp_github_create_pull_request"] == "deny"

    perm_external = build_permission({"allowed_external_systems": ["jira"]}, tools_index=idx, permission_mode="workspace_full_access")
    assert perm_external["efp_github_create_pull_request"] == "deny"


def test_full_enabled_tools_contract_init_assets_merge_and_backup(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    tools = tmp_path / "tools"
    skills = tmp_path / "skills"
    state = tmp_path / "state"
    payload = _sample_tools_payload()
    _write_generator_repo(tools, tools_payload=payload)
    (skills / "create-pull-request").mkdir(parents=True, exist_ok=True)
    (skills / "create-pull-request" / "skill.md").write_text("---\nname: create-pull-request\ndescription: Create PR\ntask_tools:\n  - run_command\n  - github_get_default_branch\n  - github_create_pull_request\n---\n\nBody\n", encoding="utf-8")

    config = workspace / ".opencode" / "opencode.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"unknown_key": "keep-me", "permission": {"*": "deny"}}), encoding="utf-8")

    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_TOOLS_DIR", str(tools))
    monkeypatch.setenv("EFP_SKILLS_DIR", str(skills))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(config))

    init_assets(Settings.from_env())
    cfg = json.loads(config.read_text(encoding="utf-8"))
    assert cfg["unknown_key"] == "keep-me"
    assert "_efp_managed" in cfg
    assert "efp_github_create_pull_request" in cfg["permission"]

    config.write_text("{not-json", encoding="utf-8")
    init_assets(Settings.from_env())
    assert any(p.name.startswith("opencode.json.bak.") for p in config.parent.iterdir())


def test_full_enabled_tools_contract_skill_sync_and_invocation(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    op_skills = tmp_path / "workspace/.opencode/skills"
    state_dir = tmp_path / "state"
    cmds = tmp_path / "workspace/.opencode/commands"

    (skills_dir / "create-pull-request").mkdir(parents=True, exist_ok=True)
    (skills_dir / "create-pull-request" / "skill.md").write_text(
        "---\nname: create-pull-request\ndescription: Create PR\ntask_tools:\n  - git_clone\n  - run_command\n  - github_get_default_branch\n  - github_create_pull_request\n---\n\nBody\n",
        encoding="utf-8",
    )
    tools_index = {
        "tools": [
            {"legacy_name": "git_clone", "opencode_name": "efp_git_clone", "enabled": True},
            {"legacy_name": "run_command", "opencode_name": "efp_run_command", "enabled": True},
            {"legacy_name": "github_get_default_branch", "opencode_name": "efp_github_get_default_branch", "enabled": True},
            {"legacy_name": "github_create_pull_request", "opencode_name": "efp_github_create_pull_request", "enabled": True},
        ]
    }
    idx = sync_skills(skills_dir, op_skills, state_dir, tools_index=tools_index, opencode_commands_dir=cmds)
    entry = idx.skills[0]
    assert entry.missing_tools == []
    assert entry.missing_opencode_tools == []
    assert (cmds / "create-pull-request.md").exists()

    skill = {
        "efp_name": "create_pull_request",
        "opencode_name": "create-pull-request",
        "opencode_supported": True,
        "runtime_equivalence": True,
        "programmatic": False,
        "missing_tools": ["github_create_pull_request"],
        "missing_opencode_tools": [],
        "tool_mappings": [{"efp_name": "github_create_pull_request", "available": False, "policy_tags": ["mutation", "write", "requires_approval"]}],
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "skills-index.json").write_text(json.dumps({"skills": [skill]}), encoding="utf-8")
    cfg = tmp_path / "opencode.json"
    cfg.write_text(json.dumps({"permission": {"skill": {"create-pull-request": "allow"}}}), encoding="utf-8")
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state_dir))
    monkeypatch.setenv("OPENCODE_CONFIG", str(cfg))
    decision = evaluate_skill_invocation(Settings.from_env(), parse_slash_invocation("/create-pull-request a"))
    assert decision.allowed is False
    assert decision.reason == "missing_required_writeback_tools"


@pytest.mark.skipif(not os.getenv("EFP_TEST_TOOLS_REPO_DIR"), reason="EFP_TEST_TOOLS_REPO_DIR not set")
def test_real_tools_repo_pr11_contract():
    tools_dir = Path(os.environ["EFP_TEST_TOOLS_REPO_DIR"])
    assert (tools_dir / "manifest.yaml").exists(), tools_dir
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        idx = sync_tools(tools_dir, root / "opencode-tools", root / "state")
        legacy = {t.get("legacy_name") for t in idx.get("tools", [])}
        for name in ["git_clone", "run_command", "github_create_pull_request"]:
            assert name in legacy, name
        perm = build_permission({}, tools_index=idx, permission_mode="workspace_full_access")
        assert perm.get("efp_github_create_pull_request") == "ask"
        assert perm.get("efp_git_clone") == "ask"
