import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from shortsmaker.store import Store
from shortsmaker.jobs import Jobs
from shortsmaker.service import new_clip
from shortsmaker.media import render_key
from shortsmaker.publishing import Publisher, Connections, BufferError, cleanup_due


class Context:
    def check(self):
        pass

    def progress(self, *args):
        pass


class FakeConnections:
    def __init__(self):
        self.posts = {}
        self.created = []
        self.deleted = []
        self.fail = None

    def channels(self):
        return [
            dict(id=x, name=x, displayName=x, service=x, isQueuePaused=False)
            for x in ("instagram", "youtube", "tiktok")
        ]

    def upload(self, path, key, ctx):
        return "https://media.example/" + key

    def create(self, channel, text, due, url, privacy):
        self.created.append(channel["id"])
        if channel["id"] == self.fail:
            raise BufferError("network lost")
        p = dict(id=channel["id"], status="scheduled", dueAt=due, sentAt=None)
        self.posts[p["id"]] = p
        return p

    def post(self, id):
        if id not in self.posts:
            raise BufferError("missing", "NOT_FOUND")
        return self.posts[id]

    def delete(self, id):
        self.posts.pop(id)

    def remove_media(self, key):
        self.deleted.append(key)


def setup(tmp_path):
    store = Store(tmp_path)
    p = store.create(
        "/tmp/video.mp4",
        dict(duration=300, width=1920, height=1080, size=1, mtime_ns=1),
    )
    c = new_clip(0, 100)
    c.update(hook="매일 무겁게 하면 손해", yellow="손해", confirmed=True)
    output = tmp_path / "render.mp4"
    output.write_bytes(b"fake")
    c["render"] = dict(path=str(output), key=render_key(p, c))
    p = store.change(p["id"], lambda p: p.update(clips=[c]))
    connections = FakeConnections()
    publisher = Publisher(store, Jobs(tmp_path), connections)
    args = dict(
        clip_ids=[c["id"]],
        channel_ids=["instagram", "youtube"],
        due_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
    )
    return p, publisher, connections, args


def test_schedule_retry_never_duplicates_successful_posts(tmp_path):
    p, publisher, conn, args = setup(tmp_path)
    publisher.schedule(Context(), p["id"], args)
    publisher.schedule(Context(), p["id"], args)
    assert conn.created == ["instagram", "youtube"]
    assert len(publisher.records()) == 1


def test_unknown_outcome_blocks_retry_and_preserves_all_destinations(tmp_path):
    p, publisher, conn, args = setup(tmp_path)
    conn.fail = "youtube"
    with pytest.raises(BufferError):
        publisher.schedule(Context(), p["id"], args)
    record = publisher.records()[0]
    assert [d["status"] for d in record["deliveries"]] == ["scheduled", "unknown"]
    with pytest.raises(ValueError, match="불명확"):
        publisher.schedule(Context(), p["id"], args)
    assert conn.created == ["instagram", "youtube"]
    assert not conn.deleted


def test_first_channel_failure_keeps_pending_second_channel(tmp_path):
    p, publisher, conn, args = setup(tmp_path)
    conn.fail = "instagram"
    with pytest.raises(BufferError):
        publisher.schedule(Context(), p["id"], args)
    assert [d["status"] for d in publisher.records()[0]["deliveries"]] == [
        "unknown",
        "pending",
    ]


def test_delete_verifies_not_found_before_media_deletion(tmp_path):
    p, publisher, conn, args = setup(tmp_path)
    publisher.schedule(Context(), p["id"], args)
    publisher.delete_job(
        Context(), p["id"], {"record_id": publisher.records()[0]["id"]}
    )
    assert not conn.posts
    assert len(conn.deleted) == 1
    assert publisher.records()[0]["deleted_at"]


def test_refresh_error_never_deletes_media(tmp_path):
    p, publisher, conn, args = setup(tmp_path)
    publisher.schedule(Context(), p["id"], args)
    conn.posts = {}
    publisher.refresh()
    assert all(d["status"] == "unknown" for d in publisher.records()[0]["deliveries"])
    assert not conn.deleted


def test_new_edit_cannot_duplicate_active_reservation(tmp_path):
    p, publisher, conn, args = setup(tmp_path)
    publisher.schedule(Context(), p["id"], args)

    def modify(p):
        c = p["clips"][0]
        c["hook"] = "새 문구"
        c["yellow"] = "문구"
        c["render"]["key"] = render_key(p, c)

    publisher.store.change(p["id"], modify)
    with pytest.raises(ValueError, match="이전 편집본"):
        publisher.schedule(Context(), p["id"], args)
    assert len(conn.created) == 2


def test_cancel_after_previous_delete_response_lost(tmp_path):
    p, publisher, conn, args = setup(tmp_path)
    publisher.schedule(Context(), p["id"], args)
    conn.posts.clear()
    publisher.delete_job(
        Context(), p["id"], {"record_id": publisher.records()[0]["id"]}
    )
    assert len(conn.deleted) == 1


def test_add_tiktok_to_existing_reservation_is_idempotent(tmp_path):
    p, publisher, conn, args = setup(tmp_path)
    publisher.schedule(Context(), p["id"], args)
    before = publisher.records()[0]["deliveries"]
    args["channel_ids"] = ["tiktok"]
    publisher.schedule(Context(), p["id"], args)
    publisher.schedule(Context(), p["id"], args)
    assert conn.created == ["instagram", "youtube", "tiktok"]
    record = publisher.records()[0]
    assert record["deliveries"][:2] == before
    assert record["deliveries"][2]["status"] == "scheduled"
    assert len(publisher.records()) == 1


def test_connections_include_tiktok_and_use_video_metadata():
    conn = object.__new__(Connections)
    requests = []

    def gql(query, variables=None):
        requests.append((query, variables))
        if "organizations" in query:
            return {"account": {"organizations": [{"id": "org"}]}}
        if "channels(input" in query:
            return {"channels": [{"id": s, "service": s} for s in
                                 ("instagram", "youtube", "tiktok", "facebook")]}
        return {"createPost": {"__typename": "PostActionSuccess",
                               "post": {"id": "post", "status": "scheduled"}}}

    conn.gql = gql
    channels = conn.channels()
    assert [c["service"] for c in channels] == ["instagram", "youtube", "tiktok"]
    assert all(c["organization_id"] == "org" for c in channels)
    conn.create(channels[2], "영상 제목", "2027-01-01T00:00:00Z", "https://media.example/video.mp4")
    payload = requests[-1][1]["input"]
    assert payload["metadata"] == {"tiktok": {"isAiGenerated": False}}
    assert payload["assets"] == [{"video": {"url": "https://media.example/video.mp4"}}]
    assert payload["channelId"] == "tiktok"
    assert payload["schedulingType"] == "automatic"
    with pytest.raises(ValueError, match="지원하지 않는"):
        conn.create({"service": "facebook"}, "title", "date", "url")
