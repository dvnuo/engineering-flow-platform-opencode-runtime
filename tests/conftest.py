import asyncio
import inspect

import pytest


@pytest.fixture(autouse=True)
def isolated_runtime_paths(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(tmp_path / "opencode-data"))
    monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode" / "opencode.json"))


def pytest_pyfunc_call(pyfuncitem):
    if inspect.iscoroutinefunction(pyfuncitem.obj):
        kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(pyfuncitem.obj(**kwargs))
        finally:
            loop.close()
        return True
    return None
