def test_readiness_contract(get_json):
    status, c = get_json("/api/capabilities")
    assert status == 200
    assert c["engine"] == "opencode"
    assert isinstance(c["capabilities"], list)
    assert c["count"] >= 1

    status, s = get_json("/api/sessions")
    assert status == 200
    assert isinstance(s.get("sessions"), list)

    status, sk = get_json("/api/skills")
    assert status == 200
    assert isinstance(sk.get("skills"), list)

    status, q = get_json("/api/queue/status")
    assert status == 200
    assert q["status"] == "ok"
    assert q["engine"] == "opencode"

    status, sf = get_json("/api/server-files")
    assert status == 200
    assert isinstance(sf, (dict, list))
