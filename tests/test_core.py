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


def test_legacy_ai_movement_is_ignored_and_manual_position_is_static():
    c = new_clip(20, 110)
    c["framing"] = [dict(start=30, end=90, zoom=1.2, center=0.1, end_center=0.9)]
    assert scenes_for(c) == [dict(start=20, end=110, zoom=1.5, center=0.5)]
    c["manual_frame"] = dict(zoom=1.7, center=0.8, vertical=0.4)
    assert scenes_for(c) == [dict(start=20, end=110, **c["manual_frame"])]
    from shortsmaker.media import video_filter
    assert "pad=1080:1920:0:" in video_filter(dict(width=1920, height=1080), scenes_for(c)[0])


def test_information_suggestions_never_change_render_or_manual_edit(tmp_path, monkeypatch):
    from shortsmaker import framing
    store, p = project(tmp_path)
    jobs = Jobs(tmp_path)
    service = Service(store, jobs)
    c = new_clip(0, 10)
    c["manual_frame"] = dict(zoom=1.6, center=0.3, vertical=0.6)
    p = store.change(p["id"], lambda p: p.update(clips=[c]))
    key = render_key(p, c)
    proposal = [dict(start=0, end=10, time=5, center=0.8, zoom=1.2,
                     direction="오른쪽 정보 확인", confidence=0.9, reason="판서 잘림")]
    monkeypatch.setattr(framing, "analyze", lambda *args: proposal)
    monkeypatch.setattr(service, "source", lambda p: None)
    class Context:
        def progress(self, *args): pass
    service.framing(Context(), p["id"], {})
    updated = store.load(p["id"])
    assert updated["clips"][0]["manual_frame"] == c["manual_frame"]
    assert updated["clips"][0]["frame_suggestions"] == proposal
    assert render_key(updated, updated["clips"][0]) == key


def test_vertical_placement_and_validation(tmp_path):
    from shortsmaker.service import validate_clip
    meta = dict(width=1920, height=1080)
    assert geometry(meta, vertical=0)["y"] == 0
    assert geometry(meta, vertical=1)["y"] == 1008
    assert geometry(meta, vertical=0.5)["y"] == 504
    c = new_clip(0, 10)
    c["manual_frame"] = dict(zoom=1.5, center=0.5, vertical=float("nan"))
    with pytest.raises(ValueError): validate_clip(c, 10)


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


def test_restart_stops_only_matching_orphan_process_group(tmp_path):
    import subprocess, sys
    from shortsmaker.jobs import process_identity

    child = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(30)"], start_new_session=True
    )
    try:
        job = dict(
            id="b" * 32,
            status="running",
            project_id="p",
            created_at="1",
            kind="slow",
            args={},
            process_pid=child.pid,
            process_identity=process_identity(child.pid),
        )
        atomic_json(tmp_path / "jobs" / (job["id"] + ".json"), job)
        jobs = Jobs(tmp_path)
        child.wait(timeout=5)
        assert jobs.items[job["id"]]["status"] == "interrupted"
        assert jobs.items[job["id"]]["process_pid"] is None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_shutdown_cancels_active_job_and_leaves_retryable_state(tmp_path):
    import sys

    jobs = Jobs(tmp_path)
    jobs.handlers["slow"] = lambda ctx, p, args: ctx.run(
        [sys.executable, "-c", "import time;time.sleep(30)"]
    )
    job = jobs.submit("p", "slow")
    deadline = time.time() + 3
    while jobs.items[job["id"]]["status"] == "queued" and time.time() < deadline:
        time.sleep(0.01)
    jobs.shutdown()
    assert jobs.items[job["id"]]["status"] == "cancelled"
    assert jobs.items[job["id"]].get("process_pid") is None


def test_title_punctuation_uses_text_baseline(tmp_path):
    from PIL import Image

    target = tmp_path / "punctuation.png"
    title_image("가,", ",", target)
    image = Image.open(target)
    yellow_y = []
    white_y = []
    for y in range(150, 400):
        for x in range(400, 650):
            pixel = image.getpixel((x, y))
            if pixel[:3] == (255, 212, 0) and pixel[3] > 200:
                yellow_y.append(y)
            if pixel[:3] == (255, 255, 255) and pixel[3] > 200:
                white_y.append(y)
    assert yellow_y and white_y
    assert min(yellow_y) > min(white_y) + (max(white_y) - min(white_y)) * 0.5


def test_information_intervals_include_uncropped_graphics_and_split_gaps():
    from shortsmaker.framing import group_information
    fs = [dict(index=i, scene=0, time=t, path=f"frame-{i}.jpg") for i,t in enumerate([0,2,4,6,8,10])]
    regions = {i:dict(information_present=i in (1,2,4), left=.4, right=.6, zoom=1.5, subject="각도 설명선", reason="초록 각도선",confidence=.95) for i in range(6)}
    items = group_information([dict(start=0,end=10)],fs,regions,dict(zoom=1.5,center=.5),dict(width=1920,height=1080))
    assert [(s['start'],s['end']) for s in items] == [(1,5),(7,9)]
    assert all(s['center']==.5 and s['zoom']==1.5 for s in items)


def test_region_position_edit_affects_only_approved_interval_and_undo(tmp_path):
    store,p=project(tmp_path);jobs=Jobs(tmp_path);service=Service(store,jobs)
    c=new_clip(0,10);c['frame_suggestions']=[dict(id='test-region',start=2,end=4,zoom=1.2,center=0,time=3)]
    p=store.change(p['id'],lambda p:p.update(clips=[c]))
    initial=render_key(p,c)
    assert scenes_for(c)==[dict(start=0,end=10,zoom=1.5,center=.5)]
    p=service.edit(p['id'],dict(action='frame_region',clip_id=c['id'],suggestion_id='test-region',frame=dict(zoom=1.2,center=0,vertical=.4)))
    scenes=scenes_for(p['clips'][0])
    assert [(s['start'],s['end']) for s in scenes]==[(0,2),(2,4),(4,10)]
    assert scenes[0]['center']==scenes[-1]['center']==.5 and scenes[1]['center']==0
    assert render_key(p,p['clips'][0])!=initial
    p=service.edit(p['id'],dict(action='undo'))
    assert render_key(p,p['clips'][0])==initial
