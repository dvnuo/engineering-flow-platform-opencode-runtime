import inspect, json
from pathlib import Path
import pytest
from efp_opencode_adapter.skill_sync import KNOWN_FIELDS, sync_skills


def _write_skill(root: Path, name="demo", extra=""):
    d=root/name; d.mkdir(parents=True,exist_ok=True)
    (d/'skill.md').write_text(f"---\nname: {name}\ndescription: Demo\ntools: [a]\ntask_tools: [b]\n{extra}---\n\nBody\n",encoding='utf-8')


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
