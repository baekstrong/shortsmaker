"""Edit framing on source shots without removing or splitting video content."""

import math
from bisect import bisect_left
from fractions import Fraction
from .store import uid


def frame_time(clip, metadata, value):
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or not clip["start"] <= value <= clip["end"]):
        raise ValueError("쇼츠 안의 프레임을 선택해 주세요.")
    times = sorted({clip["start"], clip["end"],
                    *(t for s in clip.get("source_shots", []) for t in s.get("frame_times", [])
                      if clip["start"] <= t <= clip["end"])})
    if len(times) > 2:
        i = bisect_left(times, value)
        return min(times[max(0, i - 1):i + 1], key=lambda t: abs(t - value))
    try:
        fps = float(Fraction(str(metadata.get("fps", "30/1"))))
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError
    except (ValueError, ZeroDivisionError):
        fps = 30
    return min(clip["end"], max(clip["start"], round(value * fps) / fps))


def replace_frame(clip, shot, frame):
    """Replace only the selected shot, retaining both sides of crossing overrides."""
    if frame is not None and (not isinstance(frame, dict)
            or set(frame) - {"zoom", "center", "vertical"}
            or not {"zoom", "center"} <= set(frame)):
        raise ValueError("영상 위치가 올바르지 않습니다.")
    start, end = shot["start"], shot["end"]
    result = []
    for previous in clip.get("frame_overrides", []):
        if previous["end"] <= start or previous["start"] >= end:
            # A user can move this shot's old region elsewhere with the time
            # editor. Preserve that region, but free the ID for the new one.
            result.append(dict(previous, id=uid())
                          if frame is not None and previous["id"] == shot["id"]
                          else previous)
            continue
        if previous["start"] < start:
            result.append(dict(previous, id=uid(), end=start))
        if previous["end"] > end:
            result.append(dict(previous, id=uid(), start=end))
    if frame is not None:
        result.append(dict(id=shot["id"], source_shot_id=shot["id"], start=start, end=end,
                           origin="manual", **frame))
    clip["frame_overrides"] = sorted(result, key=lambda item: item["start"])


def refresh_shots(shots, times):
    for index, shot in enumerate(shots):
        frames = [t for t in times if shot["start"] <= t < shot["end"]]
        if index == 0:
            # A fractional trim starts inside the preceding decoded frame.
            # Keep its PTS so frame stepping can still return to that image.
            previous = bisect_left(times, shot["start"]) - 1
            if previous >= 0:
                frames.insert(0, times[previous])
        shot["frame_times"] = frames
        shot["time"] = max(shot["start"], frames[len(frames) // 2]) if frames else (shot["start"] + shot["end"]) / 2
        shot["thumbnail"] = f"shot-{shot['id']}-{round(shot['start'] * 1e6)}-{round(shot['end'] * 1e6)}.jpg"
        shot["boundary_kind"] = "manual"
        # Old ordinal indices no longer describe a moved boundary.
        shot.pop("start_frame", None)
        shot.pop("end_frame", None)


def edit_shot(clip, metadata, action, body):
    shots = clip.get("source_shots") or []
    if not shots:
        raise ValueError("먼저 원본 컷을 찾아 주세요.")
    times = sorted({t for s in shots for t in s.get("frame_times", [])})
    if action == "shot_split":
        at = frame_time(clip, metadata, body.get("at"))
        shot = next((s for s in shots if s["start"] < at < s["end"]), None)
        if shot is None:
            raise ValueError("기존 경계 사이의 프레임에서 컷을 나눠 주세요.")
        second = dict(shot, id=uid(), start=at)
        shot["end"] = at
        shots.insert(shots.index(shot) + 1, second)
    else:
        shot = next((s for s in shots if s["id"] == body.get("shot_id")), None)
        if shot is None:
            raise ValueError("원본 컷을 다시 선택해 주세요.")
        if action == "frame_shot":
            replace_frame(clip, shot, body.get("frame"))
            return
        index = shots.index(shot)
        if action == "shot_merge":
            if index + 1 >= len(shots):
                raise ValueError("뒤에 합칠 원본 컷이 없습니다.")
            shot["end"] = shots.pop(index + 1)["end"]
        elif action == "shot_boundary":
            edge = body.get("edge")
            if edge not in ("start", "end"):
                raise ValueError("조정할 컷의 시작 또는 끝을 선택해 주세요.")
            left_index = index - 1 if edge == "start" else index
            if not 0 <= left_index < len(shots) - 1:
                raise ValueError("쇼츠 맨 앞과 맨 뒤는 구간 편집에서 조정해 주세요.")
            left, right = shots[left_index:left_index + 2]
            at = frame_time(clip, metadata, body.get("at"))
            if not left["start"] < at < right["end"]:
                raise ValueError("양쪽 컷에 최소 한 프레임이 남도록 경계를 조정해 주세요.")
            old = right["start"]
            left["end"] = right["start"] = at
            # An applied shot and its suggestion follow their shared boundary.
            # Independently trimmed ranges retain their explicitly chosen times.
            for region in clip.get("frame_overrides", []) + (clip.get("frame_suggestions") or []):
                for key in ("start", "end"):
                    if abs(region[key] - old) < 1e-6:
                        region[key] = at
                if region["end"] <= region["start"]:
                    raise ValueError("적용된 구도가 있는 구간을 넘었습니다. 구도 시간을 먼저 조정해 주세요.")
        else:
            raise ValueError("지원하지 않는 컷 편집입니다.")
    refresh_shots(shots, times)
    clip["source_shots_manual"] = True


def trim_shots(clip):
    """A changed short range must never retain a stale, partly detected timeline."""
    clip.pop("source_shots", None)
    clip.pop("source_shots_manual", None)
