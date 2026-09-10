import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from shortsmaker.store import Store, Conflict, atomic_json
from shortsmaker.jobs import Jobs
from shortsmaker.service import Service, new_clip
from shortsmaker.media import geometry, title_image, render_key, scenes_for
from shortsmaker.publishing import cleanup_due, Publisher, BufferError


def project(tmp_path):
    store = Store(tmp_path)
    p = store.create(
        "/tmp/source.mp4",
        dict(duration=600, width=1920, height=1080, size=1, mtime_ns=1),
    )
    return store, p


def test_stale_edit_cannot_overwrite_and_undo_persists(tmp_path):
    store, p = project(tmp_path)
    jobs = Jobs(tmp_path)
    service = Service(store, jobs)
    p = service.edit(p["id"], dict(action="add", start=10, end=150, revision=0))
    cid = p["clips"][0]["id"]
    with pytest.raises(Conflict):
        service.edit(
            p["id"],
            dict(action="update", clip_id=cid, changes={"start": 20}, revision=0),
        )
    assert store.load(p["id"])["clips"][0]["start"] == 10
    p = service.edit(
        p["id"], dict(action="split", clip_id=cid, at=75, revision=p["revision"])
    )
    assert [(c["start"], c["end"]) for c in p["clips"]] == [(10, 75), (75, 150)]
    p = service.edit(p["id"], dict(action="undo", revision=p["revision"]))
    assert [(c["start"], c["end"]) for c in Store(tmp_path).load(p["id"])["clips"]] == [
        (10, 150)
    ]


def test_render_key_changes_for_visual_edits_but_not_delivery_state(tmp_path):
    _, p = project(tmp_path)
    c = new_clip(10, 100)
    c.update(hook="매일 무겁게 하면 손해입니다", yellow="손해", confirmed=True)
    key = render_key(p, c)
    c["render"] = {"path": "old.mp4"}
    c["included"] = False
    assert render_key(p, c) == key
    c["manual_frame"] = {"zoom": 1.2, "center": 0.6}
    assert render_key(p, c) != key


def test_geometry_and_title_wrap(tmp_path):
    g = geometry({"width": 1920, "height": 1080})
    assert g == dict(
        zoom=1.5, scaled_width=1620, scaled_height=912, x=270, end_x=270, y=504
    )
    assert geometry({"width": 1920, "height": 1080}, 1.5, 1)["x"] == 540
    target = tmp_path / "title.png"
    result = title_image(
        "수백 명 가르치고 알게 된\n단 하나의 차이", "하나의 차이", target
    )
    from PIL import Image

    image = Image.open(target)
    assert image.size == (1080, 1920)
    assert any(pixel[:3] == (255, 212, 0) for pixel in image.get_flattened_data())
    with pytest.raises(ValueError):
        title_image("제목", "없는 구절", target)


def test_scene_clamping_covers_modified_clip():
    c = new_clip(20, 110)
    c["framing"] = [dict(start=30, end=90, zoom=1.2, center=0.6)]
    assert [(s["start"], s["end"]) for s in scenes_for(c)] == [
        (20, 30),
        (30, 90),
        (90, 110),
    ]


def test_retention_waits_for_every_channel_success_plus_14_days():
    t = datetime(2026, 9, 10, tzinfo=timezone.utc)
    r = {
        "deliveries": [
            {"status": "sent", "sent_at": t.isoformat()},
            {"status": "scheduled", "sent_at": None},
        ]
    }
    assert not cleanup_due(r, t + timedelta(days=100))
    r["deliveries"][1] = {
        "status": "sent",
        "sent_at": (t + timedelta(days=3)).isoformat(),
    }
    assert not cleanup_due(r, t + timedelta(days=16, hours=23))
    assert cleanup_due(r, t + timedelta(days=17))
    r["deliveries"][1]["status"] = "unknown"
    assert not cleanup_due(r, t + timedelta(days=100))
    assert not cleanup_due({"deliveries": []}, t)


def test_restart_marks_interrupted_and_cancellation_stops_process(tmp_path):
    jid = "a" * 32
    atomic_json(
        tmp_path / "jobs" / f"{jid}.json",
        dict(
            id=jid,
            status="running",
            project_id="p",
            created_at="1",
            kind="slow",
            args={},
        ),
    )
    jobs = Jobs(tmp_path)
    assert jobs.items[jid]["status"] == "interrupted"
    import sys

    jobs.handlers["slow"] = lambda ctx, p, args: ctx.run(
        [sys.executable, "-c", "import time;time.sleep(30)"]
    )
    j = jobs.submit("p", "slow")
    deadline = time.time() + 3
    while jobs.items[j["id"]]["status"] == "queued" and time.time() < deadline:
        time.sleep(0.01)
    jobs.cancel(j["id"])
    deadline = time.time() + 5
    while jobs.items[j["id"]]["status"] == "running" and time.time() < deadline:
        time.sleep(0.02)
    assert jobs.items[j["id"]]["status"] == "cancelled"
