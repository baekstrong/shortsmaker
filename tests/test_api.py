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


def test_delete_project_hides_list_and_preserves_files(client, tmp_path):
    from shortsmaker.store import Store

    store = client.application.extensions["store"]
    source = tmp_path / "original.mp4"
    source.write_bytes(b"original")
    p = store.create(source, dict(duration=10, width=1920, height=1080))
    output = store.folder(p["id"]) / "output.mp4"
    output.write_bytes(b"output")
    url = f"/api/projects/{p['id']}/delete"
    assert client.post(url, json={}).status_code == 403
    assert client.post(url, json={}, headers={"X-Shortsmaker": "1"}).status_code == 200
    assert client.get("/api/state").json["projects"] == []
    assert Store(tmp_path).list() == []
    assert store.load(p["id"])["deleted_at"]
    assert source.read_bytes() == b"original"
    assert output.read_bytes() == b"output"


def test_delete_busy_project_is_rejected(client, monkeypatch):
    store = client.application.extensions["store"]
    p = store.create("/tmp/source.mp4", dict(duration=10, width=1920, height=1080))
    monkeypatch.setattr(client.application.extensions["jobs"], "busy", lambda pid: True)
    response = client.post(f"/api/projects/{p['id']}/delete", json={},
                           headers={"X-Shortsmaker": "1"})
    assert response.status_code == 409
    assert len(store.list()) == 1
    assert "deleted_at" not in store.load(p["id"])
