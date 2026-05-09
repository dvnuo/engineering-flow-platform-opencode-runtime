from efp_opencode_adapter.profile_store import ProfileOverlay, ProfileOverlayStore, build_profile_status_payload
from efp_opencode_adapter.settings import Settings


def test_profile_store_env_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_WORKSPACE_DIR', str(tmp_path/'ws'))
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    monkeypatch.setenv('OPENCODE_CONFIG', str(tmp_path/'ws/.opencode/opencode.json'))
    s=Settings.from_env()
    o=ProfileOverlay(runtime_profile_id='r',revision=1,config={},applied_at='t',generated_config_hash='h',status='applied',applied=True,env_hash='eh',env_path='/x/opencode.env',restart_performed=True,opencode_pid=123,health_ok=True)
    store=ProfileOverlayStore(s); store.save(o)
    loaded=store.load()
    assert loaded and loaded.env_hash=='eh' and loaded.opencode_pid==123
    st=build_profile_status_payload(s)
    assert st['env_hash']=='eh' and st['restart_performed'] is True
