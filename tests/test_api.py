import pytest
from app import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path)
    app.config.update(TESTING=True)
    return app.test_client()


def test_browser_mutations_require_same_origin_header(client):
    assert (
        client.post("/api/projects", json={"path": "/tmp/missing.mp4"}).status_code
        == 403
    )
    assert (
        client.post(
            "/api/projects",
            json={},
            headers={"X-Shortsmaker": "1", "Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    assert client.get("/api/state", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/api/state").status_code == 200


def test_missing_media_returns_actionable_error(client):
    r = client.post(
        "/api/projects",
        json={"path": "/tmp/no-such-video.mp4"},
        headers={"X-Shortsmaker": "1"},
    )
    assert r.status_code == 400
    assert "다운로드" in r.json["error"]


def test_secrets_are_not_exposed_by_state(client):
    data = client.get("/api/state").json
    assert set(data) == {"projects", "jobs", "models"}
    assert "gpt-6-astra" in data["models"]
