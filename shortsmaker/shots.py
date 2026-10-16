"""Detect edit boundaries from consecutive decoded frames and retain their PTS.

The time of a boundary is the timestamp of the first frame after the change.
Neither sampling intervals nor an average frame rate determine edit times.
"""

import hashlib
import json
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .store import atomic_json


CACHE_VERSION = 3
_SIZE = (320, 180)


def _fingerprint(source, clip):
    stat = source.stat()
    return dict(version=CACHE_VERSION, source=str(source), size=stat.st_size,
                mtime_ns=stat.st_mtime_ns, start=float(clip["start"]),
                end=float(clip["end"]))


def _decoded_frames(ctx, source, start, end):
    """Seek before the clip, then decode sequentially using presentation times."""
    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        cap.release()
        raise ValueError("컷 분석용 영상을 열 수 없습니다.")
    try:
        # Seeking straight to a requested time can land on a later frame. A
        # short preroll also retains the frame visible at a fractional start.
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0, start - 2) * 1000)
        previous_time = None
        before_start = None
        started = False
        while True:
            ctx.check()
            ok, frame = cap.read()
            if not ok:
                break
            at = float(cap.get(cv2.CAP_PROP_POS_MSEC)) / 1000
            if (not math.isfinite(at) or at < 0 or
                    (previous_time is not None and at <= previous_time)):
                # Guessing from average FPS silently shifts edits on VFR
                # exports. Refuse inaccurate metadata instead.
                raise ValueError("영상의 정확한 프레임 시각을 읽지 못했습니다. 영상을 다시 불러와 주세요.")
            previous_time = at
            number = max(0, int(round(cap.get(cv2.CAP_PROP_POS_FRAMES))) - 1)
            item = (at, number, frame)
            if at < start - 1e-7:
                before_start = item
                continue
            if not started:
                if before_start is not None and at > start + 1e-7:
                    yield before_start
                started = True
            if at >= end - 1e-7:
                break
            yield item
        if not started and before_start is not None:
            yield before_start
    finally:
        cap.release()


def _descriptor(frame):
    small = cv2.resize(frame, _SIZE, interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small, (3, 3), 0)
    histogram = cv2.calcHist([small], [0, 1, 2], None, [8, 8, 8], [0, 256] * 3)
    cv2.normalize(histogram, histogram)
    return small, histogram


def _difference(previous, current):
    delta = cv2.absdiff(previous[0], current[0]).mean(axis=2)
    # Local tile changes find small inserted diagrams even while the speaker
    # moves elsewhere. The subtitle strip is excluded only for this detector;
    # full-frame changes remain eligible as ordinary cuts.
    tiles = delta.reshape(9, 20, 16, 20).mean(axis=(1, 3))[:7].reshape(-1)
    return dict(mean=float(delta.mean()), fraction=float((delta > 22).mean()),
                histogram=float(cv2.compareHist(previous[1], current[1],
                                                 cv2.HISTCMP_BHATTACHARYYA)),
                tiles=tiles)


def _boundaries(rows):
    """Use temporal contrast, not a minimum cut duration, to reject motion."""
    boundaries = []
    for i in range(1, len(rows)):
        change = rows[i]["change"]
        # Nearby ordinary motion provides a local baseline. Median is robust
        # to a second nearby edit, including a one-frame or 0.1-second insert.
        neighbours = [rows[j]["change"] for j in
                      range(max(1, i - 4), min(len(rows), i + 5)) if j != i]
        baseline = float(np.median([v["mean"] for v in neighbours])) if neighbours else 0
        abrupt = change["mean"] > max(9, baseline * 3.2 + 3)
        hard_cut = ((abrupt and change["fraction"] > .18) or
                    (change["histogram"] > .35 and change["mean"] > 10 and
                     change["mean"] > baseline * 2.5 + 3))
        if hard_cut:
            boundaries.append((i, "cut"))
            continue
        local_baseline = (np.mean([v["tiles"] for v in neighbours], axis=0)
                          if neighbours else np.zeros_like(change["tiles"]))
        adjacent = [rows[j]["change"]["tiles"] for j in (i - 1, i + 1)
                    if 0 < j < len(rows)]
        if adjacent:
            # Moving subjects/PIP animations can repeatedly enter previously
            # quiet tiles. Their adjacent-frame motion must also be exceeded;
            # the mean also includes motion in low-FPS inserts with repeats.
            local_baseline = np.maximum(local_baseline, np.max(adjacent, axis=0))
        surprised = ((change["tiles"] > 5.5) &
                     (change["tiles"] > local_baseline * 3.5 + 3))
        # Several independently changed tiles reject compression noise and
        # small facial gestures. No duration floor hides a brief graphic.
        if int(surprised.sum()) >= 3 and change["fraction"] > .006:
            boundaries.append((i, "graphic"))
    return boundaries


def ensure_thumbnails(ctx, source, clip, scenes, folder):
    """Render missing thumbnails for manually split/moved source shots."""
    pending = sorted((s for s in scenes if not (Path(folder) / s["thumbnail"]).is_file()),
                     key=lambda s: s["time"])
    if not pending:
        return
    previous = None
    for at, _, frame in _decoded_frames(ctx, source, clip["start"], clip["end"]):
        while pending and pending[0]["time"] <= at + 1e-6:
            scene = pending.pop(0)
            selected = frame
            if at >= scene["end"] - 1e-7 and previous is not None:
                selected = previous
            image = Image.fromarray(cv2.cvtColor(selected, cv2.COLOR_BGR2RGB))
            image.thumbnail((960, 540))
            image.save(Path(folder) / scene["thumbnail"], quality=88)
        if not pending:
            return
        previous = frame
    if pending and previous is not None:
        for scene in pending:
            image = Image.fromarray(cv2.cvtColor(previous, cv2.COLOR_BGR2RGB))
            image.thumbnail((960, 540))
            image.save(Path(folder) / scene["thumbnail"], quality=88)


def detect_shots(ctx, project, clip, folder):
    """Return source-time cuts, representative images, and exact frame times.

    ``frame_times`` contains decoded presentation timestamps, including the
    frame already visible when the clip starts between frames. ``start`` and
    ``end`` retain the clip's original trim boundaries. ``end_frame`` is
    exclusive; frame numbers/FPS are display metadata, PTS is authoritative.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    source = Path(project["source"]).expanduser().resolve()
    start, end = float(clip["start"]), float(clip["end"])
    if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end:
        raise ValueError("컷 분석할 영상 구간이 올바르지 않습니다.")
    fingerprint = _fingerprint(source, clip)
    cache_path = folder / "shots.json"
    try:
        cached = json.loads(cache_path.read_text())
        if (cached.get("fingerprint") == fingerprint and cached.get("shots") and
                all((folder / s["thumbnail"]).is_file() for s in cached["shots"])):
            ctx.check()
            return cached["shots"]
    except (OSError, ValueError, KeyError, TypeError):
        pass

    rows, previous = [], None
    for at, number, frame in _decoded_frames(ctx, source, start, end):
        current = _descriptor(frame)
        rows.append(dict(time=at, frame=number,
                         change=_difference(previous, current) if previous else None))
        previous = current
    if not rows:
        raise ValueError("컷을 확인할 프레임을 읽지 못했습니다. 원본을 확인해 주세요.")
    boundaries = [(0, "clip_start"), *_boundaries(rows)]
    intervals = [(a, b, kind) for (a, kind), (b, _) in
                 zip(boundaries, [*boundaries[1:], (len(rows), "clip_end")])]
    steps = np.diff([row["time"] for row in rows])
    fps = float(1 / np.median(steps)) if len(steps) else 30.0
    identity_prefix = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()[:12]
    shots = []
    targets = {}
    for a, b, kind in intervals:
        shot_start = start if a == 0 else rows[a]["time"]
        shot_end = end if b == len(rows) else rows[b]["time"]
        if shot_end <= shot_start:
            continue
        # Use an actual frame near the middle of each detected cut, never an
        # interpolated time between samples to infer its edit boundaries.
        representative = (a + b - 1) // 2
        at = rows[representative]["time"]
        identity = hashlib.sha256(f"{identity_prefix}:{shot_start:.9f}:{shot_end:.9f}".encode()).hexdigest()[:16]
        thumbnail = f"shot-{identity}.jpg"
        shots.append(dict(id=identity, start=start if a == 0 else round(shot_start, 6),
                          end=end if b == len(rows) else round(shot_end, 6),
                          time=round(max(start, at), 6), thumbnail=thumbnail,
                          start_frame=rows[a]["frame"],
                          end_frame=rows[b - 1]["frame"] + 1,
                          frame_times=[round(row["time"], 6) for row in rows[a:b]],
                          fps=round(fps, 6), frame_accurate=True, boundary_kind=kind))
        targets[round(at, 7)] = folder / thumbnail
    # A sequential second pass avoids FPS-based seeks picking an adjacent
    # frame in VFR footage. It also bounds memory for long, high-res clips.
    for at, _, frame in _decoded_frames(ctx, source, start, end):
        target = targets.pop(round(at, 7), None)
        if target is not None:
            image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            image.thumbnail((960, 540))
            image.save(target, quality=88)
        if not targets:
            break
    if targets:
        raise ValueError("일부 컷의 대표 프레임을 읽지 못했습니다. 다시 시도해 주세요.")
    atomic_json(cache_path, dict(fingerprint=fingerprint, shots=shots))
    return shots
