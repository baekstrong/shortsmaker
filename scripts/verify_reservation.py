"""Explicit live reservation test. Future-only, private YouTube, immediate cleanup.
Usage: .venv/bin/python scripts/verify_reservation.py --live PROJECT_ID CLIP_ID
No credentials printed; the running local app owns external calls and the journal.
"""

import argparse
import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--live", action="store_true", required=True)
parser.add_argument("project_id")
parser.add_argument("clip_id")
args = parser.parse_args()
BASE = "http://127.0.0.1:5099"


def api(path, body=None):
    req = urllib.request.Request(
        BASE + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Shortsmaker": "1"},
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


def wait(job_id):
    last = None
    while True:
        state = api("/api/state")
        job = next(j for j in state["jobs"] if j["id"] == job_id)
        if job["status"] != last:
            print(job["kind"], job["status"], flush=True)
            last = job["status"]
        if job["status"] not in ("queued", "running"):
            if job["status"] != "succeeded":
                raise RuntimeError(job["message"])
            return job
        time.sleep(1)


before = api("/api/reservations")["records"]
if any(
    r["project_id"] == args.project_id
    and r["clip_id"] == args.clip_id
    and not r.get("deleted_at")
    for r in before
):
    raise SystemExit("기존 예약이 있는 쇼츠는 테스트하지 않습니다.")
known = {r["id"] for r in before}
channels = api("/api/channels")["channels"]
body = dict(
    clip_ids=[args.clip_id],
    channel_ids=[c["id"] for c in channels],
    due_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
    spacing_minutes=60,
    youtube_privacy="private",
)
report = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "project_id": args.project_id,
    "clip_id": args.clip_id,
    "due_at": body["due_at"],
}
path = Path("data/verification/app-reservation-test.json")
path.parent.mkdir(parents=True, exist_ok=True)
try:
    job = api(f"/api/projects/{args.project_id}/jobs/schedule", body)
    report["schedule_job_id"] = job["id"]
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    wait(job["id"])
    current = api("/api/reservations")["records"]
    created = [
        r
        for r in current
        if r["id"] not in known
        and r["project_id"] == args.project_id
        and r["clip_id"] == args.clip_id
    ]
    report["scheduled_records"] = created
    assert created and all(
        d["status"] == "scheduled" for r in created for d in r["deliveries"]
    )
    print("두 채널 scheduled 확인", flush=True)
finally:
    # A Ctrl-C while queued must not leave a reservation job that executes later.
    if report.get("schedule_job_id"):
        pending = next(
            j for j in api("/api/state")["jobs"] if j["id"] == report["schedule_job_id"]
        )
        if pending["status"] in ("queued", "running"):
            api(f'/api/jobs/{pending["id"]}/cancel', {})
            try:
                wait(pending["id"])
            except RuntimeError:
                pass
    current = api("/api/reservations")["records"]
    for record in current:
        if (
            record["id"] not in known
            and record["project_id"] == args.project_id
            and record["clip_id"] == args.clip_id
            and not record.get("deleted_at")
        ):
            job = api(
                f"/api/projects/{args.project_id}/jobs/delete_reservation",
                {"record_id": record["id"]},
            )
            wait(job["id"])
    report["final_records"] = [
        r for r in api("/api/reservations")["records"] if r["id"] not in known
    ]
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    assert all(
        r.get("deleted_at") and all(d["status"] == "cancelled" for d in r["deliveries"])
        for r in report["final_records"]
    )
    print("예약 취소 및 R2 정리 확인 · 실제 발행 없음", flush=True)
