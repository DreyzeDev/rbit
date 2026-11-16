"""Mobile/LAN API contract used by the native rbit iPhone app."""

from __future__ import annotations

from rbit.cli import API_OPERATIONS, _mobile_links, build_openapi_spec


def test_openapi_exposes_mobile_endpoints() -> None:
    paths = build_openapi_spec()["paths"]
    assert "/v1/status" in paths
    assert "/v1/agents" in paths
    assert "/v1/sessions/{id}/abort" in paths
    assert "post" in paths["/v1/sessions/{id}/abort"]
    turn_schema = paths["/v1/turns"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert "effort" in turn_schema["properties"]
    assert "agent" in turn_schema["properties"]


def test_api_operations_include_mobile_actions() -> None:
    assert API_OPERATIONS["status.get"] == ("GET", "/v1/status")
    assert API_OPERATIONS["agents.list"] == ("GET", "/v1/agents")
    assert API_OPERATIONS["sessions.abort"] == ("POST", "/v1/sessions/{id}/abort")


def test_mobile_links_use_lan_ip_and_token(monkeypatch) -> None:
    monkeypatch.setattr("rbit.cli._lan_ips", lambda: ["192.168.1.50", "10.0.0.7"])
    links = _mobile_links(8333, "secret-token")
    assert len(links) == 2
    assert links[0][0] == "http://192.168.1.50:8333"
    assert links[0][1] == (
        "rbit://connect?url=http%3A%2F%2F192.168.1.50%3A8333&token=secret-token"
    )
    assert "192.168.1.50" not in links[1][1]
    assert "10.0.0.7" in links[1][1]
