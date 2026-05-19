import inspect, json
from pathlib import Path
import pytest
from efp_opencode_adapter.skill_sync import KNOWN_FIELDS, sync_skills


def _write_skill(
    root: Path,
    name="demo",
    extra="",
    body="Body",
    entry_filename="skill.md",
):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    entry = d / entry_filename
    entry.write_text(
        f"---\nname: {name}\ndescription: Demo\ntools: [a]\ntask_tools: [b]\n{extra}---\n\n{body}\n",
        encoding="utf-8",
    )
    return entry


def test_sync_skills_signature_has_no_tools_index():
    assert 'tools_index' not in inspect.signature(sync_skills).parameters


def test_old_tools_index_argument_is_rejected(tmp_path):
    with pytest.raises(TypeError):
        sync_skills(tmp_path/'skills', tmp_path/'out', tmp_path/'state', tools_index={"tools":[]})


def test_stale_tools_index_file_is_ignored(tmp_path):
    skills=tmp_path/'skills'; out=tmp_path/'out'; st=tmp_path/'state'; st.mkdir(parents=True)
    (st/'tools-index.json').write_text('{"tools":[{"opencode_name":"efp_fake"}]}',encoding='utf-8')
    _write_skill(skills)
    idx=sync_skills(skills,out,st)
    payload=json.loads((st/'skills-index.json').read_text())
    s=payload['skills'][0]
    for k in ('tool_mappings','opencode_tools','missing_tools','missing_opencode_tools'):
        assert k not in s
    md=(out/'demo'/'SKILL.md').read_text()
    assert 'efp_fake' not in md and 'tool_mappings' not in md


def test_malformed_tools_index_file_is_ignored(tmp_path):
    skills=tmp_path/'skills'; out=tmp_path/'out'; st=tmp_path/'state'; st.mkdir(parents=True)
    (st/'tools-index.json').write_text('{not-json',encoding='utf-8')
    _write_skill(skills)
    idx=sync_skills(skills,out,st)
    assert len(idx.skills)==1


def test_frontmatter_tool_mapping_and_opencode_tools_are_ignored(tmp_path):
    skills=tmp_path/'skills'; out=tmp_path/'out'; st=tmp_path/'state'
    _write_skill(skills, extra='tool_mapping: {a: efp_a}\nopencode_tools: [efp_a]\n')
    sync_skills(skills,out,st)
    s=json.loads((st/'skills-index.json').read_text())['skills'][0]
    for k in ('tool_mappings','opencode_tools','missing_tools','missing_opencode_tools'):
        assert k not in s
    md=(out/'demo'/'SKILL.md').read_text()
    assert 'opencode_tools' not in md and 'tool_mappings' not in md


def test_original_tools_and_task_tools_are_informational_only(tmp_path):
    skills=tmp_path/'skills'; out=tmp_path/'out'; st=tmp_path/'state'
    _write_skill(skills)
    sync_skills(skills,out,st)
    s=json.loads((st/'skills-index.json').read_text())['skills'][0]
    assert s['tools']==['a'] and s['task_tools']==['b']
    md=(out/'demo'/'SKILL.md').read_text()
    assert 'informational only' in md.lower()


def test_commands_generated_without_external_wrapper_index_when_supported_and_equivalent(tmp_path):
    skills=tmp_path/'skills'; out=tmp_path/'out'; st=tmp_path/'state'
    _write_skill(skills)
    sync_skills(skills,out,st)
    assert (out.parent/'commands'/'demo.md').exists()


def test_known_fields_does_not_include_removed_external_mapping_fields():
    assert "tool_mapping" not in KNOWN_FIELDS
    assert "opencode_tools" not in KNOWN_FIELDS


def test_generated_skill_prompt_does_not_mention_removed_external_tool_contract(tmp_path):
    skills=tmp_path/'skills'; out=tmp_path/'out'; st=tmp_path/'state'
    _write_skill(skills)
    sync_skills(skills, out, st)
    md=(out/'demo'/'SKILL.md').read_text(encoding='utf-8')
    for forbidden in ('external-tools', 'tools-index', 'tool_mapping', 'opencode_tools', 'wrapper mapping', 'missing_tools', 'missing_opencode_tools'):
        assert forbidden not in md
    for required in ('informational only', 'OpenCode built-in', 'runtime profile', 'permission policy'):
        assert required in md


def test_discovers_uppercase_SKILL_md(tmp_path):
    skills = tmp_path / "skills"
    out = tmp_path / "out"
    st = tmp_path / "state"
    _write_skill(skills, entry_filename="SKILL.md")

    sync_skills(skills, out, st)

    assert (out / "demo" / "SKILL.md").exists()
    payload = json.loads((st / "skills-index.json").read_text(encoding="utf-8"))
    assert payload["skills"][0]["source_path"].endswith("SKILL.md")


def test_lowercase_skill_md_remains_supported(tmp_path):
    skills = tmp_path / "skills"
    out = tmp_path / "out"
    st = tmp_path / "state"
    _write_skill(skills, entry_filename="skill.md")

    sync_skills(skills, out, st)

    assert (out / "demo" / "SKILL.md").exists()
    assert not (out / "demo" / "skill.md").exists()


def test_prefers_uppercase_when_both_exist(tmp_path):
    skills = tmp_path / "skills"
    out = tmp_path / "out"
    st = tmp_path / "state"
    _write_skill(skills, body="UPPER BODY", entry_filename="SKILL.md")
    _write_skill(skills, body="LOWER BODY", entry_filename="skill.md")

    with pytest.warns(UserWarning, match="both SKILL.md and skill.md exist; using SKILL.md"):
        idx = sync_skills(skills, out, st)

    md = (out / "demo" / "SKILL.md").read_text(encoding="utf-8")
    assert "UPPER BODY" in md
    assert "LOWER BODY" not in md
    assert any("both SKILL.md and skill.md exist; using SKILL.md" in w for w in idx.warnings)


def test_copies_directory_skill_resources(tmp_path):
    skills = tmp_path / "skills"
    out = tmp_path / "out"
    st = tmp_path / "state"
    _write_skill(skills, entry_filename="SKILL.md")
    (skills / "demo" / "scripts").mkdir()
    (skills / "demo" / "templates").mkdir()
    (skills / "demo" / "reference").mkdir()
    (skills / "demo" / "examples").mkdir()
    (skills / "demo" / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")
    (skills / "demo" / "templates" / "comment.md").write_text("template\n", encoding="utf-8")
    (skills / "demo" / "reference" / "rubric.md").write_text("rubric\n", encoding="utf-8")
    (skills / "demo" / "examples" / "sample.json").write_text("{}\n", encoding="utf-8")

    sync_skills(skills, out, st)

    assert (out / "demo" / "scripts" / "run.py").exists()
    assert (out / "demo" / "templates" / "comment.md").exists()
    assert (out / "demo" / "reference" / "rubric.md").exists()
    assert (out / "demo" / "examples" / "sample.json").exists()
    assert not (out / "demo" / "skill.md").exists()
    payload = json.loads((st / "skills-index.json").read_text(encoding="utf-8"))
    assert payload["skills"][0]["resource_files"] == [
        "scripts/run.py",
        "templates/comment.md",
        "reference/rubric.md",
        "examples/sample.json",
    ]


def test_generated_skill_contains_absolute_resource_base_and_resource_list(tmp_path):
    skills = tmp_path / "skills"
    out = tmp_path / "out"
    st = tmp_path / "state"
    _write_skill(skills, entry_filename="SKILL.md")
    (skills / "demo" / "scripts").mkdir()
    (skills / "demo" / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")

    sync_skills(skills, out, st)

    target_dir = (out / "demo").resolve()
    md = (out / "demo" / "SKILL.md").read_text(encoding="utf-8")
    assert str(target_dir) in md
    assert "scripts/run.py" in md
    assert "For Bash commands, prefer absolute paths" in md
    assert "Do not run `python3 scripts/...` from `/workspace`" in md


def test_does_not_copy_entry_files_as_resources(tmp_path):
    skills = tmp_path / "skills"
    out = tmp_path / "out"
    st = tmp_path / "state"
    _write_skill(skills, entry_filename="SKILL.md")
    _write_skill(skills, entry_filename="skill.md")

    with pytest.warns(UserWarning, match="both SKILL.md and skill.md exist; using SKILL.md"):
        sync_skills(skills, out, st)

    assert sorted(p.name for p in (out / "demo").iterdir()) == ["SKILL.md"]
    assert not (out / "demo" / "skill.md").exists()


def test_removes_stale_resource_files_on_resync(tmp_path):
    skills = tmp_path / "skills"
    out = tmp_path / "out"
    st = tmp_path / "state"
    _write_skill(skills, entry_filename="SKILL.md")
    (skills / "demo" / "scripts").mkdir()
    source_script = skills / "demo" / "scripts" / "run.py"
    source_script.write_text("print('ok')\n", encoding="utf-8")

    sync_skills(skills, out, st)
    assert (out / "demo" / "scripts" / "run.py").exists()

    source_script.unlink()
    sync_skills(skills, out, st)

    assert not (out / "demo" / "scripts" / "run.py").exists()


def test_does_not_delete_unmanaged_target_skill_dir(tmp_path):
    skills = tmp_path / "skills"
    out = tmp_path / "out"
    st = tmp_path / "state"
    _write_skill(skills, entry_filename="SKILL.md")
    target = out / "demo"
    target.mkdir(parents=True)
    marker = target / "SKILL.md"
    marker.write_text("hand written OpenCode skill\n", encoding="utf-8")

    with pytest.raises(ValueError, match="not managed by EFP"):
        sync_skills(skills, out, st)

    assert marker.read_text(encoding="utf-8") == "hand written OpenCode skill\n"


def test_skips_symlink_resource(tmp_path):
    skills = tmp_path / "skills"
    out = tmp_path / "out"
    st = tmp_path / "state"
    outside = tmp_path / "outside.py"
    outside.write_text("print('outside')\n", encoding="utf-8")
    _write_skill(skills, entry_filename="SKILL.md")
    (skills / "demo" / "scripts").mkdir()
    (skills / "demo" / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")
    (skills / "demo" / "scripts" / "link.py").symlink_to(outside)

    with pytest.warns(UserWarning, match="symlink skipped"):
        idx = sync_skills(skills, out, st)

    assert not (out / "demo" / "scripts" / "link.py").exists()
    assert (out / "demo" / "scripts" / "run.py").exists()
    assert any("symlink skipped" in w for w in idx.warnings)


def test_flat_markdown_skill_has_no_resources(tmp_path):
    skills = tmp_path / "skills"
    out = tmp_path / "out"
    st = tmp_path / "state"
    skills.mkdir(parents=True)
    (skills / "demo.md").write_text("---\nname: demo\ndescription: Demo\n---\n\nBody\n", encoding="utf-8")
    (skills / "scripts").mkdir()
    (skills / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")

    sync_skills(skills, out, st)

    assert (out / "demo" / "SKILL.md").exists()
    assert not (out / "demo" / "scripts" / "run.py").exists()
    payload = json.loads((st / "skills-index.json").read_text(encoding="utf-8"))
    assert payload["skills"][0]["resource_files"] == []
