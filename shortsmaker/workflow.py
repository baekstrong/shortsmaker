"""Two user gates: automatic preparation, then an immutable approved calendar."""

from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import json

from .media import render_key
from .service import validate_clip, export_folder
from .store import atomic_json, now, uid
from .publishing import timestamp

KST = ZoneInfo("Asia/Seoul")


def noon(day):
    return datetime.combine(day, time(12), KST)


def calendar_days(clips, start, occupied, at=None):
    at = at or datetime.now(KST)
    day = date.fromisoformat(start) if start else at.astimezone(KST).date() + timedelta(days=1)
    if noon(day) < at + timedelta(minutes=10):
        raise ValueError("첫 예약일은 아직 지나지 않은 낮 12시로 선택해 주세요.")
    items = []
    for i, clip in enumerate(clips):
        attempts = 0
        while day.isoformat() in occupied:
            day += timedelta(days=1)
            attempts += 1
            if attempts > 730:
                raise ValueError("예약 가능한 날짜를 찾지 못했습니다.")
        items.append(dict(clip_id=clip["id"], title=clip["hook"], number=i + 1,
                          date=day.isoformat(), due_at=noon(day).isoformat()))
        day += timedelta(days=1)
    return items


class Workflow:
    def __init__(self, store, jobs, service, publisher):
        self.store, self.jobs, self.service, self.publisher = store, jobs, service, publisher
        self.root = store.root / "plans"
        self.root.mkdir(parents=True, exist_ok=True)
        jobs.handlers.update(prepare=self.prepare, publish_plan=self.publish)

    def load(self, plan_id):
        if len(plan_id) != 32 or any(c not in "0123456789abcdef" for c in plan_id):
            raise ValueError("예약 달력을 찾을 수 없습니다.")
        return json.loads((self.root / (plan_id + ".json")).read_text())

    def save(self, plan):
        atomic_json(self.root / (plan["id"] + ".json"), plan)

    def event(self, ctx, stage):
        events = list(ctx.job.get("stage_events", []))
        events.append(dict(id=uid(), stage=stage, at=now()))
        ctx.manager.update(ctx.job["id"], stage_events=events)

    def prepare(self, ctx, pid, args):
        p = self.store.load(pid)
        if args.get("resume_revision") is not None and p["revision"] != args["resume_revision"]:
            raise ValueError("중단 후 편집 내용이 바뀌었습니다. 개별 단계를 실행하거나 자동 준비를 새로 시작해 주세요.")
        completed = list(args.get("completed", []))
        try:
            for stage, label in (("analyze", "내용 분석·분할"), ("hooks", "후킹 제안"), ("framing", "그림·글 확인")):
                ctx.check()
                if stage in completed:
                    continue
                ctx.progress(f"자동 준비 · {label}")
                getattr(self.service, stage)(ctx, pid, {})
                completed.append(stage)
                args.update(completed=completed, resume_revision=self.store.load(pid)["revision"])
                ctx.manager.update(ctx.job["id"], args=args)
                self.event(ctx, stage)
        finally:
            args["resume_revision"] = self.store.load(pid)["revision"]
            ctx.manager.update(ctx.job["id"], args=args)

    def selected(self, p):
        clips = [c for c in p["clips"] if c["included"]]
        if not clips:
            raise ValueError("예약할 쇼츠를 체크해 주세요.")
        for c in clips:
            validate_clip(c, p["metadata"]["duration"])
            if not c.get("hook", "").strip() or c["end"] - c["start"] > 180.001:
                raise ValueError("포함된 쇼츠의 문구와 3분 이하 구간을 먼저 확인해 주세요.")
        return clips

    def channels(self, ids=None):
        channels = self.publisher.connections.channels()
        if ids is not None:
            channels = [c for c in channels if c["id"] in ids]
            if len(channels) != len(set(ids)):
                raise ValueError("연결된 발행 채널을 다시 확인해 주세요.")
        if not channels or any(c["isQueuePaused"] for c in channels):
            raise ValueError("발행 가능한 인스타·유튜브 채널이 없거나 대기열이 일시정지 상태입니다.")
        return channels

    def occupied(self, channels, remote, ignore=None):
        """Include remote posts, uncertain local intents, and approved queued plans."""
        ids = {c["id"] for c in channels}
        ignore = ignore or {}
        own_clips = set(ignore.get("keys", {}))
        records = self.publisher.records()
        own = [r for r in records if r["project_id"] == ignore.get("project_id")
               and r["clip_id"] in own_clips and r["render_key"] == ignore["keys"][r["clip_id"]]
               and not r.get("deleted_at")]
        own_ids = {d["post_id"] for r in own for d in r["deliveries"] if d.get("post_id")}
        rows = []
        def add(due, title, source, channel):
            if due and channel in ids:
                rows.append(dict(date=timestamp(due).astimezone(KST).date().isoformat(),
                                 title=title, source=source, channel_id=channel))
        for post in remote:
            if post["id"] not in own_ids:
                add(post.get("dueAt"), post.get("text", "기존 예약"), "Buffer", post["channelId"])
        for r in records:
            if r.get("deleted_at") or r in own:
                continue
            for d in r["deliveries"]:
                if d["status"] not in ("cancelled", "rejected"):
                    add(d.get("due_at"), r["title"], "앱 예약", d["channel_id"])
        for job in self.jobs.list():
            if job["kind"] != "publish_plan" or job["status"] not in ("queued", "running"):
                continue
            other_id = job["args"].get("plan_id")
            if other_id == ignore.get("id"):
                continue
            plan = self.load(other_id)
            for item in plan["items"]:
                for channel in plan["channel_ids"]:
                    add(item["due_at"], item["title"], "인코딩·예약 진행 중", channel)
        # The same post can appear in Buffer and the local journal.
        unique = {(r["date"], r["title"], r["channel_id"]): r for r in rows}
        return list(unique.values())

    def make_plan(self, pid, args):
        p = self.store.load(pid)
        clips = self.selected(p)
        channels = self.channels(args.get("channel_ids"))
        remote = self.publisher.connections.scheduled_posts(channels)
        privacy = args.get("youtube_privacy", "public")
        if privacy not in ("public", "unlisted", "private"):
            raise ValueError("YouTube 공개 범위를 확인해 주세요.")
        with self.jobs.lock:
            if self.jobs.busy(pid) or self.store.load(pid)["revision"] != p["revision"]:
                raise ValueError("편집 또는 작업 상태가 바뀌었습니다. 달력을 다시 열어 주세요.")
            if any(r["project_id"] == pid and r["clip_id"] in {c["id"] for c in clips}
                   and not r.get("deleted_at") for r in self.publisher.records()):
                raise ValueError("선택한 쇼츠에 기존 예약 기록이 있습니다. 예약된 쇼츠는 체크 해제하거나 기존 예약 작업을 재시도해 주세요.")
            existing = self.occupied(channels, remote)
            items = calendar_days(clips, args.get("start_date"), {r["date"] for r in existing})
            plan = dict(id=uid(), project_id=pid, status="draft", created_at=now(),
                        revision=p["revision"], keys={c["id"]: render_key(p, c) for c in clips},
                        items=items, channel_ids=[c["id"] for c in channels], channels=channels,
                        youtube_privacy=privacy, timezone="Asia/Seoul", existing=existing,
                        start_date=args.get("start_date") or items[0]["date"])
            self.save(plan)
            return plan

    def check_content(self, plan):
        p = self.store.load(plan["project_id"])
        export_folder(p)
        clips = self.selected(p)
        if {c["id"]: render_key(p, c) for c in clips} != plan["keys"]:
            raise ValueError("달력을 만든 뒤 영상 편집이나 선택이 바뀌었습니다. 달력을 다시 확인해 주세요.")
        if [c["id"] for c in clips] != [r["clip_id"] for r in plan["items"]]:
            raise ValueError("영상 순서가 바뀌었습니다. 달력을 다시 확인해 주세요.")
        return p

    def check_dates(self, plan, channels, remote):
        existing = self.occupied(channels, remote, ignore=plan)
        blocked = {r["date"] for r in existing}
        for item in plan["items"]:
            if timestamp(item["due_at"]) < datetime.now(KST) + timedelta(minutes=10):
                raise ValueError("예약 시각이 임박하거나 지났습니다. 날짜를 자동 변경하지 않았습니다. 달력을 다시 확인해 주세요.")
            if item["date"] in blocked:
                raise ValueError(f"{item['date']}에 다른 예약이 생겼습니다. 달력을 다시 확인해 주세요.")

    def confirm(self, pid, plan_id):
        plan = self.load(plan_id)
        if plan["project_id"] != pid:
            raise ValueError("다른 프로젝트의 달력입니다.")
        if plan.get("job_id"):
            return self.jobs.items[plan["job_id"]]
        channels = self.channels(plan["channel_ids"])
        remote = self.publisher.connections.scheduled_posts(channels)
        with self.jobs.lock:
            plan = self.load(plan_id)
            if plan.get("job_id"):
                return self.jobs.items[plan["job_id"]]
            if self.jobs.busy(pid):
                raise ValueError("진행 중인 작업이 끝난 뒤 확정해 주세요.")
            self.check_content(plan)
            self.check_dates(plan, channels, remote)
            def approve(p):
                for c in p["clips"]:
                    if c["id"] in plan["keys"]:
                        c["confirmed"] = True
            self.store.change(pid, approve, history=True)
            plan.update(status="confirmed", approved_at=now())
            self.save(plan)
            job = self.jobs.submit(pid, "publish_plan", {"plan_id": plan_id})
            plan["job_id"] = job["id"]
            self.save(plan)
            return job

    def publish(self, ctx, pid, args):
        plan = self.load(args["plan_id"])
        if plan["project_id"] != pid or plan["status"] not in ("confirmed", "completed"):
            raise ValueError("먼저 예약 달력을 확인하고 확정해 주세요.")
        self.check_content(plan)
        channels = self.channels(plan["channel_ids"])
        self.check_dates(plan, channels, self.publisher.connections.scheduled_posts(channels))
        ids = list(plan["keys"])
        ctx.progress("1/2 · 확정한 쇼츠 인코딩")
        self.service.encode(ctx, pid, {"clip_ids": ids})  # Current completed files are reused.
        self.event(ctx, "encode")
        ctx.check()
        self.check_content(plan)
        self.check_dates(plan, channels, self.publisher.connections.scheduled_posts(channels))
        ctx.progress("2/2 · 달력에 확정한 날짜로 업로드·예약")
        self.publisher.schedule(ctx, pid, dict(clip_ids=ids, channel_ids=plan["channel_ids"],
                               schedule=plan["items"], youtube_privacy=plan["youtube_privacy"]))
        self.event(ctx, "schedule")
        plan.update(status="completed", completed_at=now())
        self.save(plan)
