from efp_opencode_adapter.opencode_status_adapter import is_status_active, normalize_status_type


def test_thin_status_does_not_treat_negative_status_as_active():
    for value in [
        "not-running",
        "not_running",
        "not-busy",
        "not_busy",
        "inactive",
        {"state": "not-running"},
    ]:
        assert normalize_status_type(value) == "idle"
        assert is_status_active(value) is False


def test_thin_status_strict_exact_mapping():
    assert normalize_status_type("running") == "busy"
    assert normalize_status_type("busy") == "busy"
    assert normalize_status_type("retry") == "retry"
    assert normalize_status_type("idle") == "idle"
    assert normalize_status_type("unknown") == "unknown"
