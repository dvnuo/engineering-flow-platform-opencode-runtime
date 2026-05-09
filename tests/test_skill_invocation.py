import json

from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.skill_invocation import evaluate_skill_invocation, parse_slash_invocation, resolve_skill


def _setup(tmp_path, monkeypatch, skills, permission):
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "skills-index.json").write_text(json.dumps({"skills": skills}), encoding="utf-8")
    cfg = tmp_path / "opencode.json"
    cfg.write_text(json.dumps({"permission": permission}), encoding="utf-8")
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(cfg))
    return Settings.from_env()


def test_parse_slash():
    inv = parse_slash_invocation("/java-cucumber-generator hello world")
    assert inv.skill_name == "java-cucumber-generator"
    assert inv.arguments == "hello world"
    inv2 = parse_slash_invocation("/java_cucumber_generator hello")
    assert inv2.skill_name == "java-cucumber-generator"
    assert parse_slash_invocation("hello /x") is None


def test_resolve_and_decisions(tmp_path, monkeypatch):
    skill = {"efp_name": "java_cucumber_generator", "opencode_name": "java-cucumber-generator", "opencode_supported": True, "runtime_equivalence": True, "programmatic": False, "missing_tools": [], "missing_opencode_tools": []}
    settings = _setup(tmp_path, monkeypatch, [skill], {"skill": {"java-cucumber-generator": "allow"}})
    assert resolve_skill(settings, "java-cucumber-generator")
    assert resolve_skill(settings, "java_cucumber_generator")
    inv = parse_slash_invocation("/java-cucumber-generator hi")
    assert evaluate_skill_invocation(settings, inv).allowed is True

    denied = _setup(tmp_path, monkeypatch, [skill], {"skill": {"*": "deny"}})
    assert evaluate_skill_invocation(denied, inv).reason == "permission_denied"

    prog = dict(skill, programmatic=True, runtime_equivalence=False)
    prog_settings = _setup(tmp_path, monkeypatch, [prog], {"skill": {"java-cucumber-generator": "allow"}})
    assert evaluate_skill_invocation(prog_settings, inv).reason == "programmatic_skill_requires_opencode_wrapper"

    missing = dict(skill, missing_tools=["x"])
    miss_settings = _setup(tmp_path, monkeypatch, [missing], {"skill": {"java-cucumber-generator": "allow"}})
    assert evaluate_skill_invocation(miss_settings, inv).reason == "missing_required_tools"
