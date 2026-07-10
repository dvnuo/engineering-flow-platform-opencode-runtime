from pathlib import Path

from efp_opencode_adapter.profile_store import ProfileOverlay, ProfileOverlayStore, build_profile_status_payload
from efp_opencode_adapter.settings import Settings


def test_profile_store_env_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_WORKSPACE_DIR', str(tmp_path/'ws'))
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    monkeypatch.setenv('OPENCODE_CONFIG', str(tmp_path/'ws/.opencode/opencode.json'))
    s=Settings.from_env()
    o=ProfileOverlay(runtime_profile_id='r',revision=1,config={},applied_at='t',generated_config_hash='h',env_hash='eh',env_path='/x/opencode.env',aws_configured=True)
    store=ProfileOverlayStore(s); store.save(o)
    loaded=store.load()
    assert loaded and loaded.env_hash=='eh' and loaded.revision==1
    assert loaded.aws_configured is True
    st=build_profile_status_payload(s)
    assert st['env_hash']=='eh' and st['revision']==1
    assert st['aws_configured'] is True
    # Apply-lifecycle semantics are gone: boot record only.
    for removed in ('status','pending_restart','applied','restart_performed','opencode_pid','health_ok','last_apply_error','restart_required'):
        assert removed not in st


def test_profile_store_load_treats_inaccessible_overlay_as_missing(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_WORKSPACE_DIR', str(tmp_path/'ws'))
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    monkeypatch.setenv('OPENCODE_CONFIG', str(tmp_path/'ws/.opencode/opencode.json'))
    store = ProfileOverlayStore(Settings.from_env())
    denied_path = store.path
    original_exists = Path.exists

    def fake_exists(path):
        if path == denied_path:
            raise PermissionError("denied")
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", fake_exists)

    assert store.load() is None
