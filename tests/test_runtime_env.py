import os
from efp_opencode_adapter.runtime_env import build_runtime_env_from_config, redact_env_for_status, write_runtime_env_file
from efp_opencode_adapter.settings import Settings


def test_runtime_env_build_and_redact(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_WORKSPACE_DIR', str(tmp_path/'ws'))
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    monkeypatch.setenv('OPENCODE_CONFIG', str(tmp_path/'ws/.opencode/opencode.json'))
    s=Settings.from_env()
    cfg={"github":{"api_token":"t","api_base_url":"https://api.github.com"},"jira":{"instances":[{"enabled":True,"url":"https://j/","username":"u","token":"x","project":"P"}]},"confluence":{"instances":[{"enabled":True,"url":"https://c/","token":"y"}]},"proxy":{"enabled":True,"url":"http://h:1","username":"a","password":"b"},"git":{"author_name":"n","author_email":"e@x"},"debug":{"enabled":True,"log_level":"DEBUG"}}
    r=build_runtime_env_from_config(s,cfg)
    assert r.env['JIRA_EMAIL']=='u' and r.env['JIRA_API_TOKEN']=='x' and 'JIRA_TOKEN' not in r.env
    assert r.env['CONFLUENCE_TOKEN']=='y' and 'CONFLUENCE_API_TOKEN' not in r.env
    p=write_runtime_env_file(s,r.env)
    assert oct(os.stat(p).st_mode & 0o777)=='0o600'
    red=redact_env_for_status(r.env)
    assert red['GITHUB_TOKEN'] is True
