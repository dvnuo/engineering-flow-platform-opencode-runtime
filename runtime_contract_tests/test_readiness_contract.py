def test_readiness_contract(get_json):
    _, c = get_json('/api/capabilities'); assert c['engine'] == 'opencode' and isinstance(c['capabilities'], list) and c['count'] >= 1
    _, s = get_json('/api/sessions'); assert isinstance(s.get('sessions'), list)
    _, sk = get_json('/api/skills'); assert isinstance(sk.get('skills'), list)
    _, q = get_json('/api/queue/status'); assert q['status'] == 'ok' and q['engine'] == 'opencode'
    _, sf = get_json('/api/server-files'); assert isinstance(sf, (dict, list))
