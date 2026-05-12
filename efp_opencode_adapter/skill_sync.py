from __future__ import annotations
import argparse, hashlib, json, re, shutil, warnings
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import yaml
from .settings import Settings

KNOWN_FIELDS={"name","description","version","owner","triggers","tools","task_tools","risk_level","output_format","when_to_use","model","hooks","kind","runtime","execution","runtime_compat","opencode_supported","opencode","tool_mapping","opencode_tools","opencode_runtime_equivalence"}
GENERATED_MARKER="This skill was generated from an EFP skill asset."
GENERATED_COMMAND_MARKER="This command was generated from an EFP skill asset."

@dataclass(frozen=True)
class SkillIndexEntry:
    efp_name:str; opencode_name:str; description:str; tools:list[str]; task_tools:list[str]; risk_level:str|None; source_path:str; target_path:str
    opencode_compatibility:str="prompt_only"; runtime_equivalence:bool=True; programmatic:bool=False; opencode_supported:bool=True; compatibility_warnings:list[str]=field(default_factory=list)

@dataclass(frozen=True)
class SkillsIndex:
    generated_at:str; skills:list[SkillIndexEntry]; warnings:list[str]=field(default_factory=list)
    def to_json_dict(self)->dict: return {"generated_at":self.generated_at,"skills":[asdict(x) for x in self.skills],"warnings":list(self.warnings)}

def _split_frontmatter(text:str):
    if not text.startswith('---\n'): return None
    i=text.find('\n---\n',4)
    if i==-1:return None
    p=yaml.safe_load(text[4:i]) or {}
    if not isinstance(p,dict): raise ValueError('frontmatter must be a mapping')
    return p,text[i+5:]

def _validate_list_field(frontmatter:dict, source_path:Path, field_name:str)->list[str]:
    v=frontmatter.get(field_name,[])
    if v is None:return []
    if isinstance(v,list):
        if not all(isinstance(x,str) for x in v): raise ValueError(f"{source_path}: {field_name} must be list[str]")
        return v
    if v=="": return []
    raise ValueError(f"{source_path}: {field_name} must be list[str], got {type(v).__name__}")

def normalize_skill_name(raw_name:str,fallback_seed:str)->str:
    n=re.sub(r'[^a-z0-9-]','-',re.sub(r'\s+','-',raw_name.lower().replace('_','-'))).strip('-')
    return re.sub(r'-+','-',n) or f"skill-{hashlib.sha256(fallback_seed.encode()).hexdigest()[:12]}"

def _discover_skill_files(skills_dir:Path)->list[Path]:
    out=[]
    for child in skills_dir.iterdir():
        if child.is_dir() and not child.name.startswith('.'):
            c=child/'skill.md'
            if c.exists(): out.append(c)
    for md in skills_dir.glob('*.md'):
        if _split_frontmatter(md.read_text(encoding='utf-8')) is not None: out.append(md)
    return sorted(out,key=str)

def _is_programmatic_skill(source_path:Path, fm:dict[str,Any])->bool:
    op=fm.get('opencode') if isinstance(fm.get('opencode'),dict) else {}
    return any([(source_path.parent/'skill.py').exists(),source_path.with_suffix('.py').exists(),str(fm.get('kind','')).lower() in {'programmatic','hybrid'},str(fm.get('execution','')).lower()=='python',str(fm.get('runtime','')).lower()=='python',str(op.get('execution','')).lower()=='python'])

def _opencode_supported(frontmatter:dict[str,Any])->bool:
    if frontmatter.get('opencode_supported') is False:return False
    rc=frontmatter.get('runtime_compat')
    if isinstance(rc,list) and not ({'opencode','all'} & {str(x).lower() for x in rc}): return False
    if isinstance(rc,str) and rc.lower() not in {'opencode','all'}: return False
    op=frontmatter.get('opencode') if isinstance(frontmatter.get('opencode'),dict) else {}
    return not (op.get('compatible') is False or op.get('supported') is False)

def _render_skill_markdown(opencode_name:str, entry:SkillIndexEntry, frontmatter:dict, body:str)->str:
    meta={"efp_name":entry.efp_name,"efp_tools":','.join(entry.tools),"efp_task_tools":','.join(entry.task_tools),"efp_source_path":entry.source_path}
    fm=yaml.safe_dump({"name":opencode_name,"description":entry.description,"license":"internal","compatibility":"opencode","metadata":meta},sort_keys=False,allow_unicode=True).strip()
    warn=("## Compatibility Warnings\n"+"\n".join([f"- {w}" for w in entry.compatibility_warnings])+"\n\n") if entry.compatibility_warnings else ""
    return f"---\n{fm}\n---\n\n# {entry.description or entry.efp_name}\n\n{GENERATED_MARKER}\n\nThis generated skill is a prompt asset.\n\nThe runtime tool surface comes from OpenCode built-in tools, OpenCode MCP tools when enabled by OpenCode itself, skills, runtime profile, and permission policy.\n\nThis adapter does not provide EFP external-tools wrappers or tools-index mapping.\n\nAny EFP tools/task_tools metadata in the source skill is informational only.\n\n{warn}{body.rstrip()}\n"

def _render_command_markdown(entry:SkillIndexEntry)->str:
    return f"---\ndescription: Run EFP skill {entry.opencode_name}\nagent: efp-main\n---\n\n{GENERATED_COMMAND_MARKER}\n\nRun the OpenCode agent skill `{entry.opencode_name}`.\n"

def sync_skills(skills_dir:Path, opencode_skills_dir:Path, state_dir:Path, opencode_commands_dir:Path|None=None)->SkillsIndex:
    opencode_skills_dir.mkdir(parents=True, exist_ok=True); state_dir.mkdir(parents=True, exist_ok=True)
    if opencode_commands_dir is None: opencode_commands_dir=opencode_skills_dir.parent/'commands'
    opencode_commands_dir.mkdir(parents=True,exist_ok=True)
    warnings_list=[]; skills=[]
    if not skills_dir.exists():
        msg=f"skills directory does not exist: {skills_dir}"; warnings.warn(msg); warnings_list.append(msg)
        idx=SkillsIndex(datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),[],warnings_list)
        (state_dir/'skills-index.json').write_text(json.dumps(idx.to_json_dict(),ensure_ascii=False,indent=2)+'\n',encoding='utf-8'); return idx
    discovered=_discover_skill_files(skills_dir)
    current=set()
    for sp in discovered:
        p=_split_frontmatter(sp.read_text(encoding='utf-8')); 
        if p: current.add(normalize_skill_name(str((p[0].get('name') or (sp.parent.name if sp.name=='skill.md' else sp.stem))),str(sp.resolve())))
    for child in opencode_skills_dir.iterdir():
        if child.is_dir() and child.name not in current and (child/'SKILL.md').exists() and GENERATED_MARKER in (child/'SKILL.md').read_text(encoding='utf-8'): shutil.rmtree(child)

    generated=set(); name_to_source={}
    for sp in discovered:
        p=_split_frontmatter(sp.read_text(encoding='utf-8'))
        if p is None: continue
        fm,body=p
        raw=str(fm.get('name') or (sp.parent.name if sp.name=='skill.md' else sp.stem)); name=normalize_skill_name(raw,str(sp.resolve()))
        if name in name_to_source: raise ValueError(f"duplicate normalized skill name: normalized name={name}, source_path={name_to_source[name]}, source_path={sp}")
        name_to_source[name]=sp
        desc=(fm.get('description') or '').strip() if isinstance(fm.get('description',''),str) else ''
        if not desc: desc=f"EFP skill {raw}"; warnings_list.append(f"{sp}: missing description, fallback to '{desc}'")
        tools=_validate_list_field(fm,sp,'tools'); task=_validate_list_field(fm,sp,'task_tools'); risk=fm.get('risk_level') or 'unknown'
        t=opencode_skills_dir/name/'SKILL.md'; t.parent.mkdir(parents=True,exist_ok=True)
        programmatic=_is_programmatic_skill(sp,fm); supported=_opencode_supported(fm)
        compat=[]; op='prompt_only'; eq=True
        if not supported: op='unsupported'; eq=False; compat.append('skill is marked unsupported for OpenCode runtime')
        elif programmatic: op='programmatic_prompt_only'; eq=False; compat.append('EFP skill.py is not executed by the OpenCode adapter; generated skill is prompt-only')
        e=SkillIndexEntry(raw,name,desc,tools,task,risk,str(sp.resolve()),str(t.resolve()),op,eq,programmatic,supported,compat)
        t.write_text(_render_skill_markdown(name,e,fm,body),encoding='utf-8'); skills.append(e)
        if e.opencode_supported and e.runtime_equivalence:
            (opencode_commands_dir/f"{e.opencode_name}.md").write_text(_render_command_markdown(e),encoding='utf-8'); generated.add(e.opencode_name)

    for cmd in opencode_commands_dir.glob('*.md'):
        if cmd.stem not in generated and GENERATED_COMMAND_MARKER in cmd.read_text(encoding='utf-8'): cmd.unlink()
    idx=SkillsIndex(datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),skills,warnings_list)
    (state_dir/'skills-index.json').write_text(json.dumps(idx.to_json_dict(),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return idx

def main()->None:
    s=Settings.from_env(); p=argparse.ArgumentParser(description='Sync EFP skills into OpenCode skills')
    p.add_argument('--skills-dir',type=Path,default=s.skills_dir); p.add_argument('--opencode-skills-dir',type=Path,default=s.workspace_dir/'.opencode'/'skills'); p.add_argument('--state-dir',type=Path,default=s.adapter_state_dir)
    a=p.parse_args(); idx=sync_skills(a.skills_dir,a.opencode_skills_dir,a.state_dir); print(f"synced {len(idx.skills)} skills")

if __name__=='__main__': main()
