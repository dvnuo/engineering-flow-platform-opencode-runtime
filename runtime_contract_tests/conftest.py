import json
import os
import urllib.request

import pytest


@pytest.fixture
def base_url():
    url = os.getenv("RUNTIME_BASE_URL")
    if not url:
        pytest.skip("RUNTIME_BASE_URL is required for live contract tests")
    return url.rstrip("/")


def _request_json(method, url, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


@pytest.fixture
def get_json(base_url):
    return lambda path: _request_json("GET", f"{base_url}{path}")


@pytest.fixture
def post_json(base_url):
    return lambda path, payload: _request_json("POST", f"{base_url}{path}", payload)
