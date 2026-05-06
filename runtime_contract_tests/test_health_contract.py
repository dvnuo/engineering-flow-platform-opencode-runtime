def test_health_contract(get_json):
    status, body = get_json('/health')
    assert status == 200
    assert body["status"] == "ok"
    assert body["engine"] == "opencode"
    assert "opencode_version" in body
    if body.get("opencode", {}).get("version") is not None:
        assert body["opencode_version"] == body["opencode"]["version"]


def test_actuator_health_contract(get_json):
    status, body = get_json('/actuator/health')
    assert status == 200 and body['engine'] == 'opencode'
