def test_health_contract(get_json):
    status, body = get_json('/health')
    assert status == 200 and body['status'] == 'ok' and body['engine'] == 'opencode' and body['opencode_version'] == '1.14.29'


def test_actuator_health_contract(get_json):
    status, body = get_json('/actuator/health')
    assert status == 200 and body['engine'] == 'opencode'
