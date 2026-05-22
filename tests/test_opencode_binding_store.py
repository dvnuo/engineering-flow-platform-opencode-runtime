from efp_opencode_adapter.opencode_binding_store import OpenCodeBindingStore


def test_opencode_binding_store_crud_contract(tmp_path):
    store = OpenCodeBindingStore(tmp_path / "bindings.json")

    created = store.create("agent-1", "ses-1", title="New chat")

    assert created.portal_conversation_id.startswith("pc_")
    assert created.agent_id == "agent-1"
    assert created.opencode_session_id == "ses-1"
    assert created.source == "opencode"
    assert created.schema_version == "opencode_conversation_binding.v1"
    assert store.get(created.portal_conversation_id) == created
    assert store.list("agent-1") == [created]
    assert store.list("agent-2") == []

    renamed = store.update_title(created.portal_conversation_id, "Renamed")
    assert renamed.title == "Renamed"
    assert renamed.updated_at >= created.updated_at

    replaced = store.replace_opencode_session(created.portal_conversation_id, "ses-2")
    assert replaced.opencode_session_id == "ses-2"

    archived = store.archive(created.portal_conversation_id)
    assert archived.archived_at
    assert store.list("agent-1") == []
    assert store.list("agent-1", include_archived=True)[0].portal_conversation_id == created.portal_conversation_id

    reloaded = OpenCodeBindingStore(tmp_path / "bindings.json")
    assert reloaded.get(created.portal_conversation_id).opencode_session_id == "ses-2"
