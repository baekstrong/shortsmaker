"""Application operations; each completed stage is checkpointed independently."""

import copy
import json
import math
import sys
import shutil
import tempfile
from pathlib import Path
from . import ai, media
from .store import uid


def export_folder(project):
    value = project.get("export_dir")
    if not value:
        raise ValueError("인코딩 전에 저장 폴더를 설정해 주세요.")
    folder = Path(value).expanduser()
    if not folder.is_absolute() or not folder.is_dir():
        raise ValueError("저장 폴더를 찾을 수 없습니다. 이 Mac에서 저장 위치를 다시 설정해 주세요.")
    try:
        with tempfile.TemporaryFile(dir=folder):
            pass
    except OSError as exc:
        raise ValueError("저장 폴더에 쓸 수 없습니다. 다른 위치를 설정해 주세요.") from exc
    return folder.resolve()


def new_clip(start, end, title="", reason=""):
    return dict(
        id=uid(),
        start=float(start),
        end=float(end),
        title=title,
        reason=reason,
        included=True,
        hooks=[],
        recommended_index=None,
        recommendation_reason="",
        hook="",
        yellow="",
        confirmed=False,
        font_size=80,
        framing=[],
        frame_suggestions=None,
        frame_overrides=[],
        manual_frame=None,
        render=None,
    )


def validate_clip(clip, duration):
    a, b = clip.get("start"), clip.get("end")
    if (
        not all(
            isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
            for x in (a, b)
        )
        or not 0 <= a < b <= duration + 0.01
    ):
        raise ValueError("시작·끝 시간이 영상 범위를 벗어났습니다.")
    if len(clip.get("hook", "")) > 100 or len(clip.get("hook", "").splitlines()) > 2:
        raise ValueError("후킹은 최대 두 줄, 100자까지입니다.")
    if clip.get("yellow") and clip["yellow"] not in clip.get("hook", ""):
        raise ValueError("강조 구절이 후킹 문구에 포함되어야 합니다.")
    if clip.get("confirmed") and not clip.get("hook", "").strip():
        raise ValueError("후킹 문구를 입력한 뒤 확정해 주세요.")
    if not 40 <= float(clip.get("font_size", 80)) <= 100:
        raise ValueError("글자 크기는 40~100 범위입니다.")
    frame = clip.get("manual_frame")
    if frame and (
        not all(
            isinstance(frame.get(k), (float, int)) and math.isfinite(frame[k])
            for k in ("zoom", "center")
        )
        or not 1 <= frame["zoom"] <= 2
        or not 0 <= frame["center"] <= 1
        or not isinstance(frame.get("vertical", 0.5), (int, float))
        or not math.isfinite(frame.get("vertical", 0.5))
        or not 0 <= frame.get("vertical", 0.5) <= 1
    ):
        raise ValueError("확대율이나 영상 위치가 올바르지 않습니다.")

    last = clip["start"]
    ids = set()
    for f in sorted(clip.get("frame_overrides", []), key=lambda f: f["start"]):
        if (not all(isinstance(f.get(k), (int, float)) and math.isfinite(f[k]) for k in ("start", "end", "zoom", "center"))
            or not last <= f["start"] < f["end"] <= clip["end"] + 0.001
            or not 1 <= f["zoom"] <= 2 or not 0 <= f["center"] <= 1
            or not isinstance(f.get("vertical", .5), (int, float))
            or not math.isfinite(f.get("vertical", .5)) or not 0 <= f.get("vertical", .5) <= 1
            or not isinstance(f.get("id"), str) or f["id"] in ids):
            raise ValueError("설명 구간의 시간이나 위치가 올바르지 않습니다.")
        ids.add(f["id"])
        last = f["end"]


class Service:
    def __init__(self, store, jobs):
        self.store, self.jobs = store, jobs
        self._source_checks = set()
        for kind in ("analyze", "hooks", "framing", "encode"):
            jobs.handlers[kind] = getattr(self, kind)

    def source(self, p):
        path = media.existing_path(p["source"])
        s = path.stat()
        signature = (str(path), s.st_size, s.st_mtime_ns, s.st_ctime_ns,
                     json.dumps(p["metadata"], sort_keys=True))
        if signature in self._source_checks:
            p["source"] = str(path)
            return path
        if (
            s.st_size != p["metadata"]["size"]
            or s.st_mtime_ns != p["metadata"]["mtime_ns"]
        ):
            # Older imports stored whole seconds; file copies restore nanoseconds.
            old = p["metadata"]["mtime_ns"]
            precision_only = (s.st_size == p["metadata"]["size"] and
                              old % 1_000_000_000 == 0 and
                              old // 1_000_000_000 == s.st_mtime_ns // 1_000_000_000)
            if not precision_only or not self.same_media(p["metadata"], media.probe(path)):
                raise ValueError("원본의 위치 또는 파일 정보가 달라졌습니다. 미리보기 아래 ‘원본 다시 연결’로 같은 영상을 선택해 주세요.")
        self._source_checks.add(signature)
        p["source"] = str(path)
        return path

    @staticmethod
    def same_media(old, new):
        return (all(old.get(k) == new.get(k) for k in
                    ("size", "width", "height", "fps", "has_audio", "hdr"))
                and abs(old["duration"] - new["duration"]) < 0.01)

    def reconnect(self, pid, value):
        path = media.existing_path(value)
        metadata = media.probe(path)
        with self.jobs.lock:
            if self.jobs.busy(pid):
                raise ValueError("진행 중인 작업이 끝난 뒤 원본을 연결해 주세요.")
            def update(p):
                if not self.same_media(p["metadata"], metadata):
                    raise ValueError("기존 원본과 크기·길이·영상 정보가 다릅니다. 같은 원본 영상을 선택해 주세요.")
                p.update(source=str(path), metadata=metadata)
            return self.store.change(pid, update)

    def selected(self, p, args):
        ids = args.get("clip_ids")
        result = [
            c for c in p["clips"] if c["included"] and (ids is None or c["id"] in ids)
        ]
        if not result:
            raise ValueError("작업할 쇼츠를 선택해 주세요.")
        return result

    def analyze(self, ctx, pid, args):
        p = self.store.load(pid)
        self.source(p)
        folder = self.store.folder(pid)
        transcript_file = folder / f"transcript-{p['stt_model']}.json"
        if not transcript_file.exists():
            ctx.progress(
                "내용 분석용 음성 인식 준비 중 · 첫 실행은 모델을 다운로드합니다.", 0
            )

            def report(line):
                try:
                    v = json.loads(line)
                    if "progress" in v:
                        ctx.progress(v["message"], v["progress"])
                except (ValueError, TypeError):
                    pass

            ctx.run(
                [
                    sys.executable,
                    "-m",
                    "shortsmaker.transcribe",
                    p["source"],
                    str(transcript_file),
                    "--model",
                    p["stt_model"],
                ],
                cwd=Path(__file__).resolve().parent.parent,
                timeout=14400,
                on_line=report,
            )
        transcript = json.loads(transcript_file.read_text())
        if not transcript:
            raise ValueError("인식된 발화가 없습니다. 음성 트랙을 확인해 주세요.")
        p = self.store.change(pid, lambda p: p.update(transcript=transcript))
        ctx.progress("Astra가 주제별 분할 지점을 제안하고 있습니다.")
        result = ai.split(ctx, p, folder / "ai-cache")
        clips = [
            new_clip(c["start"], c["end"], c["title"], c["reason"])
            for c in result["clips"]
        ]
        self.store.change(
            pid,
            lambda p: p.update(clips=clips, summary=result["summary"]),
            history=True,
        )

    def hooks(self, ctx, pid, args):
        p = self.store.load(pid)
        clips = self.selected(p, args)
        for i, clip in enumerate(clips):
            ctx.progress(
                f"후킹 후보 생성 · {i+1}/{len(clips)} · {clip['title']}",
                i * 100 / len(clips),
            )
            result = ai.hooks(
                ctx,
                p,
                clip,
                self.store.folder(pid) / "ai-cache",
                args.get("fresh", False),
            )

            def save(project):
                c = next(c for c in project["clips"] if c["id"] == clip["id"])
                c.update(
                    hooks=result["hooks"],
                    recommended_index=result["recommended_index"],
                    recommendation_reason=result["reason"],
                    hook_analysis={k: result.get(k, "") for k in ("audience_problem", "content_evidence")},
                    hook_version=2,
                )
                if not c["confirmed"] or args.get("replace_selected", False):
                    recommended = result["hooks"][result["recommended_index"]]
                    c.update(
                        hook=recommended["text"], yellow=recommended["yellow_phrase"], confirmed=False
                    )

            self.store.change(pid, save, history=True)

    def framing(self, ctx, pid, args):
        from . import framing

        p = self.store.load(pid)
        self.source(p)
        for clip in self.selected(p, args):
            ctx.progress("그림·글 잘림 확인 · " + clip["title"])
            result = framing.analyze(
                ctx,
                p,
                clip,
                self.store.folder(pid) / "frames" / clip["id"],
                self.store.folder(pid) / "ai-cache",
            )

            def save(project):
                c = next(c for c in project["clips"] if c["id"] == clip["id"])
                c.update(frame_suggestions=result, frame_analysis_version=2)
                framing.apply_recommendations(c)
                validate_clip(c, project["metadata"]["duration"])

            self.store.change(pid, save, history=True)

    def encode(self, ctx, pid, args):
        p = self.store.load(pid)
        export_dir = export_folder(p)
        self.source(p)
        for clip in self.selected(p, args):
            render = media.export_clip(ctx, p, clip, self.store.folder(pid))

            filename = f"{p['clips'].index(clip)+1:02d} {Path(render['path']).stem} {render['key'][:6]} {clip['id'][:8]}.mp4"
            destination = export_dir / filename
            if not destination.exists():
                temporary = export_dir / (uid() + ".tmp")
                try:
                    shutil.copy2(render["path"], temporary)
                    temporary.replace(destination)
                finally:
                    temporary.unlink(missing_ok=True)
            render["export_path"] = str(destination)

            def save(project):
                c = next(c for c in project["clips"] if c["id"] == clip["id"])
                c["render"] = render

            self.store.change(pid, save)

    def edit(self, pid, body):
        with self.jobs.lock:
            return self._edit(pid, body)

    def _edit(self, pid, body):
        if self.jobs.busy(pid):
            raise ValueError(
                "작업 진행 중에는 수정할 수 없습니다. 완료하거나 중단해 주세요."
            )

        def mutate(p):
            action = body.get("action", "update")
            if action == "settings":
                if body["model"] not in ai.models():
                    raise ValueError("사용 가능한 모델을 선택해 주세요.")
                if body["effort"] not in ("low", "medium", "high", "xhigh") or body[
                    "stt_model"
                ] not in ("small", "medium", "large-v3"):
                    raise ValueError("분석 설정이 올바르지 않습니다.")
                p.update({k: body[k] for k in ("model", "effort", "stt_model")})
                return
            if action == "undo":
                if not p.get("history"):
                    raise ValueError("되돌릴 편집이 없습니다.")
                p["clips"] = p["history"].pop()
                return
            p["history"] = (p.get("history", []) + [copy.deepcopy(p["clips"])])[-30:]
            if action == "add":
                c = new_clip(body["start"], body["end"], "직접 추가한 구간")
                validate_clip(c, p["metadata"]["duration"])
                p["clips"].append(c)
                p["clips"].sort(key=lambda c: c["start"])
                return
            c = next((c for c in p["clips"] if c["id"] == body.get("clip_id")), None)
            if c is None:
                raise ValueError("쇼츠를 찾을 수 없습니다.")
            if action == "frame_region_time":
                sid = body["suggestion_id"]
                suggestion = next((s for s in c.get("frame_suggestions") or [] if s.get("id") == sid), None)
                applied = next((f for f in c.get("frame_overrides", []) if f["id"] == sid), None)
                if suggestion is None and applied is None:
                    raise ValueError("설명 구간을 다시 선택해 주세요.")
                start, end = body.get("start"), body.get("end")
                if (not all(isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t) for t in (start, end))
                        or not c["start"] <= start < end <= c["end"]):
                    raise ValueError("위치 적용 시간은 쇼츠 범위 안에서 시작보다 끝이 늦어야 합니다.")
                others = [f for f in c.get("frame_overrides", []) if f["id"] != sid]
                if any(start < f["end"] and end > f["start"] for f in others):
                    raise ValueError("다른 위치 적용 구간과 시간이 겹칩니다. 시작·끝 시간을 조정해 주세요.")
                frame = applied or suggestion
                others.append(dict(id=sid, start=start, end=end, origin="manual",
                                   zoom=frame["zoom"], center=frame["center"], vertical=frame.get("vertical", .5)))
                c["frame_overrides"] = sorted(others, key=lambda f: f["start"])
                if suggestion is not None:
                    suggestion.update(start=start, end=end)
            elif action == "frame_region":
                sid = body["suggestion_id"]
                suggestion = next((s for s in c.get("frame_overrides", []) + (c.get("frame_suggestions") or []) if s.get("id") == sid), None)
                if suggestion is None:
                    raise ValueError("설명 구간을 다시 선택해 주세요.")
                frame = body.get("frame")
                if frame is None:
                    frame = dict(c.get("manual_frame") or dict(zoom=1.5, center=.5, vertical=.5))
                overrides = [f for f in c.get("frame_overrides", []) if f["id"] != sid]
                if frame is not None:
                    if not isinstance(frame, dict) or set(frame) - {"zoom", "center", "vertical"}:
                        raise ValueError("영상 위치가 올바르지 않습니다.")
                    start, end = max(c["start"], suggestion["start"]), min(c["end"], suggestion["end"])
                    if any(start < f["end"] and end > f["start"] for f in overrides):
                        raise ValueError("겹치는 직접 조정 구간이 있습니다. 기존 조정을 해제한 뒤 적용해 주세요.")
                    overrides.append(dict(id=sid, start=start, end=end, origin="manual", **frame))
                c["frame_overrides"] = sorted(overrides, key=lambda f: f["start"])
            elif action == "split":
                at = float(body["at"])
                if not c["start"] + 0.1 < at < c["end"] - 0.1:
                    raise ValueError("구간 안에서 분할 지점을 선택해 주세요.")
                second = new_clip(at, c["end"], c["title"] + " (2)", c["reason"])
                c.update(
                    end=at,
                    hooks=[],
                    hook="",
                    yellow="",
                    confirmed=False,
                    framing=[],
                    frame_suggestions=None,
                    render=None,
                )
                p["clips"].insert(p["clips"].index(c) + 1, second)
            elif action == "merge":
                i = p["clips"].index(c)
                if i + 1 >= len(p["clips"]):
                    raise ValueError("뒤에 합칠 구간이 없습니다.")
                other = p["clips"][i + 1]
                c.update(
                    start=min(c["start"], other["start"]),
                    end=max(c["end"], other["end"]),
                    hooks=[],
                    hook="",
                    yellow="",
                    confirmed=False,
                    framing=[],
                    frame_suggestions=None,
                    render=None,
                )
                p["clips"].pop(i + 1)
            elif action == "update":
                allowed = (
                    "start",
                    "end",
                    "title",
                    "included",
                    "hook",
                    "yellow",
                    "confirmed",
                    "font_size",
                    "manual_frame",
                )
                changes = {
                    k: v for k, v in body.get("changes", {}).items() if k in allowed
                }
                bounds = any(
                    k in changes and changes[k] != c[k] for k in ("start", "end")
                )
                if bounds:
                    c.update(hooks=[], confirmed=False, framing=[], frame_suggestions=None)
                    changes["confirmed"] = False
                c.update(changes)
            else:
                raise ValueError("지원하지 않는 편집입니다.")
            if action in ("split", "merge", "update"):
                c["frame_overrides"] = [dict(f, start=max(c["start"], f["start"]), end=min(c["end"], f["end"]))
                    for f in c.get("frame_overrides", []) if f["end"] > c["start"] and f["start"] < c["end"]]
            validate_clip(c, p["metadata"]["duration"])

        return self.store.change(pid, mutate, revision=body.get("revision"))
