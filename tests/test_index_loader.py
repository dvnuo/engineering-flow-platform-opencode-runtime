from pathlib import Path
from types import SimpleNamespace

from efp_opencode_adapter.index_loader import load_skills_index, read_json_file


def test_read_json_file_treats_permission_denied_path_as_missing(tmp_path, monkeypatch):
    denied_path = tmp_path / "skills-index.json"
    original_exists = Path.exists

    def fake_exists(path):
        if path == denied_path:
            raise PermissionError("denied")
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", fake_exists)

    assert read_json_file(denied_path) is None


def test_load_skills_index_treats_permission_denied_index_as_empty(tmp_path, monkeypatch):
    denied_path = tmp_path / "skills-index.json"
    original_exists = Path.exists

    def fake_exists(path):
        if path == denied_path:
            raise PermissionError("denied")
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", fake_exists)

    settings = SimpleNamespace(adapter_state_dir=tmp_path)
    assert load_skills_index(settings) == {"skills": []}
