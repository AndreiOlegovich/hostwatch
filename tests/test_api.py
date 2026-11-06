"""Disk and card routes: changes need the page's header, and a missing host helper is a clear 503."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from api.main import Settings, create_app

MISSING = Path("/nonexistent")


def client() -> TestClient:
    settings = Settings(claude_dir=MISSING, opencode_db=MISSING, ui_dist=MISSING, agent_socket=MISSING / "agent.sock")
    return TestClient(create_app(settings), base_url="http://127.0.0.1")


def test_changes_from_other_pages_are_refused() -> None:
    body = {"device": "/dev/sda7"}
    assert client().post("/api/mount", json=body).status_code == 403
    cross = {"X-Hostwatch": "1", "Sec-Fetch-Site": "cross-site"}
    assert client().post("/api/mount", json=body, headers=cross).status_code == 403


def test_missing_host_helper_says_how_to_start_it() -> None:
    response = client().get("/api/cards")
    assert response.status_code == 503
    assert "./hostwatch.py up" in response.json()["detail"]
    assert client().post("/api/unmount", json={"device": "/dev/sdb1"}, headers={"X-Hostwatch": "1"}).status_code == 503
