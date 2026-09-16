import json
import subprocess
import sys
import threading
import time

import pytest

from shortsmaker.jobs import Jobs, process_identity
from shortsmaker.parallel import parallel_tasks
from shortsmaker.store import atomic_json


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(.01)


def test_bounded_parallelism_and_complete_results():
    class Ctx:
        def check(self): pass
        def abort_parallel(self): pass

    barrier = threading.Barrier(2)
    lock = threading.Lock()
    active = peak = 0

    def work(i):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            if i < 2:
                barrier.wait(timeout=3)  # Would fail if requests were serialized.
            time.sleep(.01)
            return i * 2
        finally:
            with lock:
                active -= 1

    with parallel_tasks(Ctx(), work, range(8)) as results:
        result = dict(results)
    assert peak == 2
    assert result == {i: i * 2 for i in range(8)}


@pytest.mark.parametrize("action", ["cancel", "shutdown", "failure"])
def test_all_parallel_processes_stop_and_pending_requests_do_not_start(tmp_path, action):
    jobs = Jobs(tmp_path)
    started = []
    gate = threading.Event()
    def handler(ctx, pid, args):
        def work(i):
            started.append(i)
            if action == "failure" and i == 0:
                assert gate.wait(4)
                raise ValueError("original failure")
            return ctx.run([sys.executable, "-c", "import time; time.sleep(30)"])
        with parallel_tasks(ctx, work, range(6)) as results:
            list(results)
    jobs.handlers["test"] = handler
    job = jobs.submit("p", "test")
    jid = job["id"]
    wait_until(lambda: len(jobs.items[jid].get("processes", [])) == (1 if action == "failure" else 2))
    processes = list(jobs.items[jid]["processes"])
    persisted = json.loads((jobs.root / f"{jid}.json").read_text())
    assert persisted["processes"] == processes
    if action == "cancel":
        jobs.cancel(jid)
    elif action == "shutdown":
        jobs.shutdown()
    else:
        gate.set()
    wait_until(lambda: jobs.items[jid]["status"] not in ("queued", "running"))
    assert jobs.items[jid]["status"] == ("failed" if action == "failure" else "cancelled")
    if action == "failure":
        assert jobs.items[jid]["message"] == "original failure"
    assert sorted(started) == [0, 1]
    assert jobs.items[jid]["processes"] == []
    assert jobs.items[jid]["process_pid"] is None
    assert all(process_identity(p["process_pid"]) != p["process_identity"] for p in processes)
    def resumed(ctx, pid, args):
        with parallel_tasks(ctx, lambda i: (ctx.check(), i)[1], range(3)) as results:
            assert sorted(value for _, value in results) == [0, 1, 2]
    jobs.handlers["test"] = resumed
    retry = jobs.retry(jid)
    wait_until(lambda: jobs.items[retry["id"]]["status"] not in ("queued", "running"))
    assert jobs.items[retry["id"]]["status"] == "succeeded"



def test_restart_recovers_all_recorded_processes(tmp_path):
    children = [subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                                 start_new_session=True) for _ in range(2)]
    try:
        jid = "a" * 32
        atomic_json(tmp_path / "jobs" / f"{jid}.json", dict(
            id=jid, status="running", project_id="p", created_at="1", kind="test", args={},
            processes=[dict(process_pid=p.pid, process_identity=process_identity(p.pid)) for p in children],
        ))
        jobs = Jobs(tmp_path)
        assert jobs.items[jid]["status"] == "interrupted"
        assert jobs.items[jid]["processes"] == []
        for child in children:
            child.wait(timeout=2)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait()


def test_frame_parallel_matches_serial_inputs_and_output(tmp_path, monkeypatch):
    from functools import partial
    from PIL import Image
    from shortsmaker import framing

    class Ctx:
        def check(self): pass
        def abort_parallel(self): pass
        def progress(self, *args): pass

    source = tmp_path / "frame.jpg"
    Image.new("RGB", (64, 36), "green").save(source)
    frames = [dict(index=i, scene=0, time=i * 2, path=str(source)) for i in range(25)]
    scenes = [dict(start=0, end=50)]
    monkeypatch.setattr(framing, "sample_scenes", lambda *args: (scenes, frames))
    calls = {}
    order = []
    def respond(ctx, prompt, schema, cache, model, effort, images):
        path = images[0]
        offset = int(path.stem.split("-")[-1])
        calls[offset] = (prompt, schema, model, effort, path.read_bytes())
        if offset == 0:
            time.sleep(.08)
        order.append(offset)
        return dict(frames=[dict(index=i, left=.2, right=.7, confidence=.9, zoom=1.5,
                                information_present=i % 5 != 0, subject="설명", reason="그림")
                            for i in range(offset, min(offset + 12, 25))])
    monkeypatch.setattr(framing.ai, "call", respond)
    args = (Ctx(), dict(source="unused", metadata=dict(width=1920, height=1080),
                        transcript=[dict(start=0, end=50, text="동일한 발언")],
                        model="unchanged-model", effort="medium"),
            dict(start=0, end=50), tmp_path, tmp_path)
    parallel = framing.analyze(*args)
    parallel_calls = dict(calls)
    assert order[0] == 12
    calls.clear()
    monkeypatch.setattr(framing, "parallel_tasks", partial(parallel_tasks, workers=1))
    serial = framing.analyze(*args)
    assert parallel == serial
    assert calls == parallel_calls  # Same prompt, schema, model, effort and image bytes.
    assert sorted(calls) == [0, 12, 24]


def test_frame_parallel_still_rejects_missing_frames(tmp_path, monkeypatch):
    from PIL import Image
    from shortsmaker import framing
    class Ctx:
        def check(self): pass
        def abort_parallel(self): pass
        def progress(self, *args): pass
    source = tmp_path / "frame.jpg"
    Image.new("RGB", (32, 18)).save(source)
    monkeypatch.setattr(framing, "sample_scenes", lambda *args:
                        ([], [dict(index=0, scene=0, time=0, path=str(source))]))
    monkeypatch.setattr(framing.ai, "call", lambda *args, **kwargs: dict(frames=[]))
    with pytest.raises(ValueError, match="프레임 개수"):
        framing.analyze(Ctx(), dict(source="unused", metadata=dict(width=1920, height=1080),
                                  transcript=[], model="test", effort="medium"),
                        dict(start=0, end=2), tmp_path, tmp_path)
