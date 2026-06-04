import inspect, json
from pathlib import Path
import pytest
from efp_opencode_adapter.skill_sync import KNOWN_FIELDS, sync_skills


def _write_skill(root: Path, name="demo", extra="", entry_filename="skill.md", body="Body"):
    d=root/name; d.mkdir(parents=True,exist_ok=True)
    (d/entry_filename).write_text(f"---\nname: {name}\ndescription: Demo\ntools: [a]\ntask_tools: [b]\n{extra}---\n\n{body}\n",encoding='utf-8')
    return d


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


def test_invalid_frontmatter_yaml_is_warned_and_skipped(tmp_path):
    skills=tmp_path/'skills'; out=tmp_path/'out'; st=tmp_path/'state'
    bad=skills/'bad'; bad.mkdir(parents=True,exist_ok=True)
    (bad/'skill.md').write_text(
        "---\n"
        "name: bad\n"
        "description: Guides project patterns. Error Handling: If the pasted source input does not match.\n"
        "---\n\n"
        "Body\n",
        encoding='utf-8',
    )
    _write_skill(skills, name="good")

    with pytest.warns(UserWarning, match=r"(?s)bad.*/(?:SKILL|skill)\.md.*invalid skill frontmatter YAML.*Quote scalar values"):
        idx=sync_skills(skills,out,st)

    assert [x.opencode_name for x in idx.skills] == ["good"]
    assert not (out/'bad').exists()
    payload=json.loads((st/'skills-index.json').read_text(encoding='utf-8'))
    assert len(payload["skills"]) == 1
    warnings_text="\n".join(payload["warnings"]).replace("SKILL.md", "skill.md")
    assert "bad/skill.md" in warnings_text
    assert "invalid skill frontmatter YAML" in warnings_text


def test_discovers_uppercase_SKILL_md(tmp_path):
    skills=tmp_path/'skills'; out=tmp_path/'out'; st=tmp_path/'state'
    _write_skill(skills, entry_filename="SKILL.md")
    sync_skills(skills,out,st)
    payload=json.loads((st/'skills-index.json').read_text(encoding='utf-8'))
    assert (out/'demo'/'SKILL.md').exists()
    assert payload['skills'][0]['source_path'].endswith('SKILL.md')
    assert not (out/'demo'/'skill.md').exists()


def test_lowercase_skill_md_remains_supported(tmp_path):
    skills=tmp_path/'skills'; out=tmp_path/'out'; st=tmp_path/'state'
    _write_skill(skills, entry_filename="skill.md")
    sync_skills(skills,out,st)
    assert (out/'demo'/'SKILL.md').exists()
    assert not (out/'demo'/'skill.md').exists()


def test_prefers_uppercase_when_both_SKILL_md_and_skill_md_exist(tmp_path):
    skills=tmp_path/'skills'; out=tmp_path/'out'; st=tmp_path/'state'
    d=_write_skill(skills, entry_filename="SKILL.md", body="UPPER BODY")
    (d/'skill.md').write_text("---\nname: demo\ndescription: Demo\ntools: [a]\ntask_tools: [b]\n---\n\nLOWER BODY\n",encoding='utf-8')
    with pytest.warns(UserWarning, match="both SKILL.md and skill.md"):
        sync_skills(skills,out,st)
    md=(out/'demo'/'SKILL.md').read_text(encoding='utf-8')
    assert "UPPER BODY" in md
    assert "LOWER BODY" not in md
    assert not (out/'demo'/'skill.md').exists()


def test_copies_directory_skill_resources(tmp_path):
    skills=tmp_path/'skills'; out=tmp_path/'out'; st=tmp_path/'state'
    d=_write_skill(skills, entry_filename="SKILL.md")
    files={
        "scripts/run.py": "print('run')\n",
        "templates/comment.md": "comment\n",
        "reference/rubric.md": "rubric\n",
        "examples/sample.json": "{}\n",
        "data/config.json": '{"ok": true}\n',
    }
    for rel, text in files.items():
        p=d/rel; p.parent.mkdir(parents=True,exist_ok=True); p.write_text(text,encoding='utf-8')
    sync_skills(skills,out,st)
    for rel, text in files.items():
        assert (out/'demo'/rel).read_text(encoding='utf-8') == text
    md=(out/'demo'/'SKILL.md').read_text(encoding='utf-8')
    assert not (out/'demo'/'skill.md').exists()
    assert "Synced skill package resources" in md
    assert "scripts/run.py" in md
    assert "cd .opencode/skills/demo" in md
    payload=json.loads((st/'skills-index.json').read_text(encoding='utf-8'))
    assert sorted(payload['skills'][0]['resource_paths']) == sorted(files)


def test_does_not_copy_entry_files_as_resources(tmp_path):
    skills=tmp_path/'skills'; out=tmp_path/'out'; st=tmp_path/'state'
    d=_write_skill(skills, entry_filename="SKILL.md", body="UPPER BODY")
    (d/'skill.md').write_text("---\nname: demo\ndescription: Demo\n---\n\nLOWER BODY\n",encoding='utf-8')
    with pytest.warns(UserWarning, match="both SKILL.md and skill.md"):
        sync_skills(skills,out,st)
    assert (out/'demo'/'SKILL.md').exists()
    assert not (out/'demo'/'skill.md').exists()


def test_removes_stale_resource_files_on_resync(tmp_path):
    skills=tmp_path/'skills'; out=tmp_path/'out'; st=tmp_path/'state'
    d=_write_skill(skills, entry_filename="SKILL.md")
    script=d/'scripts'/'run.py'; script.parent.mkdir(parents=True,exist_ok=True); script.write_text("print('run')\n",encoding='utf-8')
    sync_skills(skills,out,st)
    assert (out/'demo'/'scripts'/'run.py').exists()
    script.unlink()
    sync_skills(skills,out,st)
    assert not (out/'demo'/'scripts'/'run.py').exists()


def test_does_not_delete_unmanaged_target_skill_dir(tmp_path):
    skills=tmp_path/'skills'; out=tmp_path/'out'; st=tmp_path/'state'
    _write_skill(skills, entry_filename="SKILL.md")
    target=out/'demo'; target.mkdir(parents=True,exist_ok=True)
    (target/'SKILL.md').write_text("manual",encoding='utf-8')
    with pytest.raises(ValueError, match="not managed by EFP"):
        sync_skills(skills,out,st)
    assert (target/'SKILL.md').read_text(encoding='utf-8') == "manual"


def test_skips_symlink_resource(tmp_path):
    skills=tmp_path/'skills'; out=tmp_path/'out'; st=tmp_path/'state'
    d=_write_skill(skills, entry_filename="SKILL.md")
    outside=tmp_path/'outside.py'; outside.write_text("print('outside')\n",encoding='utf-8')
    link=d/'scripts'/'link.py'; link.parent.mkdir(parents=True,exist_ok=True); link.symlink_to(outside)
    with pytest.warns(UserWarning, match="symlink skipped"):
        sync_skills(skills,out,st)
    assert not (out/'demo'/'scripts'/'link.py').exists()


def test_excludes_cache_git_and_ds_store_resources(tmp_path):
    skills=tmp_path/'skills'; out=tmp_path/'out'; st=tmp_path/'state'
    d=_write_skill(skills, entry_filename="SKILL.md")
    excluded=[
        ".git/config",
        "__pycache__/demo.pyc",
        ".pytest_cache/state",
        ".mypy_cache/state",
        ".DS_Store",
    ]
    for rel in excluded:
        p=d/rel; p.parent.mkdir(parents=True,exist_ok=True); p.write_text("skip\n",encoding='utf-8')
    (d/'data'/'keep.txt').parent.mkdir(parents=True,exist_ok=True)
    (d/'data'/'keep.txt').write_text("keep\n",encoding='utf-8')
    sync_skills(skills,out,st)
    for rel in excluded:
        assert not (out/'demo'/rel).exists()
    assert (out/'demo'/'data'/'keep.txt').exists()


def test_flat_markdown_skill_does_not_try_to_copy_resources(tmp_path):
    skills=tmp_path/'skills'; out=tmp_path/'out'; st=tmp_path/'state'
    skills.mkdir(parents=True,exist_ok=True)
    (skills/'flat.md').write_text("---\nname: flat\ndescription: Flat\n---\n\nBody\n",encoding='utf-8')
    (skills/'loose.txt').write_text("loose\n",encoding='utf-8')
    sync_skills(skills,out,st)
    assert (out/'flat'/'SKILL.md').exists()
    assert not (out/'flat'/'loose.txt').exists()


def test_generated_skill_instructions_prevent_workspace_root_confusion(tmp_path):
    skills=tmp_path/'skills'; out=tmp_path/'out'; st=tmp_path/'state'
    _write_skill(skills, entry_filename="SKILL.md")
    sync_skills(skills,out,st)
    md=(out/'demo'/'SKILL.md').read_text(encoding='utf-8')
    assert ".opencode/skills/demo" in md
    assert "Relative resource paths" in md
    assert "find .opencode/skills/demo" in md
    assert "python .opencode/skills/demo/scripts/" in md
