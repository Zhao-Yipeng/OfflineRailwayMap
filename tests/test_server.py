from __future__ import annotations

import http.client
import json


def test_page_is_served(client):
    status, headers, body = client.request("GET", "/app/index.html")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"OfflineRailwayMap" in body


def test_vendored_maplibre_is_served(client):
    status, headers, body = client.request("GET", "/app/vendor/maplibre-gl/maplibre-gl.js")
    assert status == 200
    assert headers["Content-Type"].startswith("text/javascript")
    assert len(body) > 100_000


def test_requests_without_token_are_rejected(client):
    status, _, _ = client.request("GET", "/app/index.html", token=False)
    assert status == 404


def test_requests_with_wrong_host_are_rejected(client):
    status, _, _ = client.request("GET", "/app/index.html", host="evil.example:80")
    assert status == 403


def test_path_traversal_is_blocked(client):
    status, _, _ = client.request("GET", "/app/../server.py")
    assert status == 404
    status, _, _ = client.request("GET", "/app/%2e%2e/server.py")
    assert status == 404


def test_unknown_route_and_wrong_method(client):
    assert client.request("GET", "/api/nope")[0] == 404
    assert client.request("GET", "/api/view")[0] == 405


def test_info(client, core):
    status, info = client.get_json("/api/info")
    assert status == 200
    assert info["app"] == "OfflineRailwayMap"
    assert info["base"] == f"/{core.server.token}"
    assert info["view"] == {"lon": 105.0, "lat": 35.0, "zoom": 4.0}


def test_view_is_clamped_and_remembered(client, core):
    status, _ = client.post_json("/api/view", {"lon": 500, "lat": 39.9, "zoom": 30})
    assert status == 200
    assert core.settings.last_view == (180.0, 39.9, 19.0)
    status, _ = client.post_json("/api/view", {"lon": "x"})
    assert status == 400


def test_open_external_accepts_only_web_links(client, hooks):
    assert client.post_json("/api/open-external", {"url": "https://www.openstreetmap.org/copyright"})[0] == 200
    assert client.post_json("/api/open-external", {"url": "file:///C:/Windows"})[0] == 400
    assert hooks.opened == ["https://www.openstreetmap.org/copyright"]


def test_event_stream_says_hello(core):
    conn = http.client.HTTPConnection("127.0.0.1", core.server.port, timeout=10)
    conn.request("GET", f"/{core.server.token}/api/events", headers={"Host": f"127.0.0.1:{core.server.port}"})
    response = conn.getresponse()
    assert response.status == 200
    assert response.getheader("Content-Type").startswith("text/event-stream")
    lines = []
    while not any(line.startswith(b"data:") for line in lines):
        lines.append(response.fp.readline())
    assert b"event: hello\n" in lines
    payload = json.loads(next(line for line in lines if line.startswith(b"data:"))[5:])
    assert "version" in payload
    core.events.publish("cells", {"n": 1})
    more = []
    while b"event: cells\n" not in more and len(more) < 20:
        more.append(response.fp.readline())
    assert b"event: cells\n" in more
    assert b"event: status\n" in lines + more, "the stream starts with the current connectivity status"
    conn.close()
