"""Actual frame decode regressions for cuts, overlays, and VFR timestamps."""

import shutil
import subprocess
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from shortsmaker import framing, shots


CTX = SimpleNamespace(check=lambda: None)


def write_video(path, frames, fps=30):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (320, 180))
    assert writer.isOpened()
    try:
        for frame in frames:
            writer.write(frame)
    finally:
        writer.release()
    return path


def solid(color):
    return np.full((180, 320, 3), color, np.uint8)


def test_detects_short_and_single_frame_cuts_without_sampling_delay(tmp_path):
    frames = [solid((20, 25, 120) if i < 17 or i >= 20 else (140, 90, 20)) for i in range(45)]
    frames[32] = solid((30, 180, 30))
    source = write_video(tmp_path / "cuts.avi", frames)
    result = shots.detect_shots(CTX, {"source": str(source)}, {"start": 0, "end": 1.5}, tmp_path / "frames")
    assert [s["start"] for s in result] == pytest.approx([0, 17 / 30, 20 / 30, 32 / 30, 33 / 30], abs=1e-6)
    assert [s["end"] for s in result] == pytest.approx([17 / 30, 20 / 30, 32 / 30, 33 / 30, 1.5], abs=1e-6)
    assert sum(len(s["frame_times"]) for s in result) == 45
    assert all((tmp_path / "frames" / s["thumbnail"]).is_file() for s in result)


def test_detects_small_graphic_while_rejecting_continuous_person_motion(tmp_path):
    frames = []
    for i in range(60):
        frame = solid((42, 44, 46))
        cv2.rectangle(frame, (180 + i, 30), (210 + i, 145), (140, 155, 175), -1)
        if 17 <= i < 23:
            cv2.line(frame, (30, 45), (85, 45), (10, 240, 10), 4)
            cv2.line(frame, (85, 45), (85, 120), (10, 240, 10), 4)
        # Ordinary changing dialogue subtitles must not produce graphic cuts.
        cv2.putText(frame, str(i // 9), (100, 172), cv2.FONT_HERSHEY_SIMPLEX, .6, (255, 255, 255), 2)
        frames.append(frame)
    source = write_video(tmp_path / "graphic.avi", frames)
    result = shots.detect_shots(CTX, {"source": str(source)}, {"start": 0, "end": 2}, tmp_path / "frames")
    assert [s["start"] for s in result] == pytest.approx([0, 17 / 30, 23 / 30], abs=1e-6)


def test_animated_pip_motion_is_not_split_into_many_cuts(tmp_path):
    frames = []
    for i in range(90):
        frame = solid((42, 44, 46))
        cv2.rectangle(frame, (200 + i // 3, 30), (230 + i // 3, 145), (140, 155, 175), -1)
        if 20 <= i < 70:
            cv2.rectangle(frame, (10, 10), (145, 140), (180, 180, 160), -1)
            x, y = int(72 + 45 * np.sin(i * .22)), int(75 + 40 * np.cos(i * .22))
            cv2.line(frame, (72, 75), (x, y), (10, 20, 30), 12)
            cv2.circle(frame, (x, y), 10, (20, 40, 120), -1)
        frames.append(frame)
    source = write_video(tmp_path / "animated.avi", frames)
    result = shots.detect_shots(CTX, {"source": str(source)}, {"start": 0, "end": 3}, tmp_path / "frames")
    assert [s["start"] for s in result] == pytest.approx([0, 20 / 30, 70 / 30], abs=1e-6)


def test_fractional_outer_trim_times_are_preserved_without_rounding(tmp_path):
    source = write_video(tmp_path / "fractional.avi", [solid((20, 40, 80)) for _ in range(30)])
    clip = dict(start=.1200000427, end=.830000004)
    result = shots.detect_shots(CTX, {"source": str(source)}, clip, tmp_path / "frames")
    assert result[0]["start"] == clip["start"]
    assert result[-1]["end"] == clip["end"]
    scene = result[0]
    frames = [dict(index=0, scene=0, time=scene["time"], path=scene["thumbnail"])]
    region = dict(information_present=True, left=.05, right=.45, zoom=1.5,
                  subject="설명 그림", reason="그림 전체 표시", confidence=.95)
    suggestions = framing.group_information(result, frames, {0: region},
                                            dict(zoom=1.5, center=.5), dict(width=1920, height=1080))
    assert suggestions[0]["start"] == clip["start"]
    assert suggestions[0]["end"] == clip["end"]


def test_cache_invalidates_for_range_version_source_and_missing_thumbnail(tmp_path, monkeypatch):
    source = write_video(tmp_path / "cache.avi", [solid((20, 40, 80)) for _ in range(30)])
    folder = tmp_path / "frames"
    project, clip = {"source": str(source)}, {"start": 0, "end": 1}
    initial = shots.detect_shots(CTX, project, clip, folder)
    original = shots._decoded_frames
    calls = []

    def counted(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(shots, "_decoded_frames", counted)
    assert shots.detect_shots(CTX, project, clip, folder) == initial
    assert calls == []
    shifted = shots.detect_shots(CTX, project, dict(start=.2, end=.8), folder)
    assert calls and shifted[0]["start"] == .2 and shifted[-1]["end"] == .8
    calls.clear()
    monkeypatch.setattr(shots, "CACHE_VERSION", shots.CACHE_VERSION + 1)
    shots.detect_shots(CTX, project, dict(start=.2, end=.8), folder)
    assert calls
    calls.clear()
    write_video(source, [solid((120, 80, 20)) for _ in range(30)])
    changed = shots.detect_shots(CTX, project, dict(start=.2, end=.8), folder)
    assert calls
    calls.clear()
    (folder / changed[0]["thumbnail"]).unlink()
    shots.detect_shots(CTX, project, dict(start=.2, end=.8), folder)
    assert calls


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_variable_frame_rate_uses_decoded_timestamps_not_average_fps(tmp_path):
    frames = [solid((20, 25, 120) if i < 30 else (140, 90, 20)) for i in range(60)]
    original = write_video(tmp_path / "cfr.avi", frames)
    source = tmp_path / "vfr.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(original),
                    "-vf", "select='lt(n,20)+gte(n,20)*not(mod(n,2))'",
                    "-fps_mode", "vfr", "-c:v", "ffv1", str(source)], check=True)
    result = shots.detect_shots(CTX, {"source": str(source)}, {"start": .51, "end": 1.9}, tmp_path / "frames")
    assert [s["start"] for s in result] == pytest.approx([.51, 1.0], abs=1e-6)
    times = [t for s in result for t in s["frame_times"]]
    assert min(np.diff(times)) < .04
    assert max(np.diff(times)) > .06
    assert result[0]["end"] == result[1]["start"] == 1.0


def test_exact_shot_suggestions_keep_cut_boundaries_and_do_not_merge():
    scenes = [dict(id="a", start=0, end=.566667, frame_accurate=True),
              dict(id="b", start=.566667, end=.666667, frame_accurate=True)]
    frames = [dict(index=0, scene=0, time=.25, path="a.jpg"),
              dict(index=1, scene=1, time=.6, path="b.jpg")]
    region = dict(information_present=True, left=.05, right=.45, zoom=1.5,
                  subject="설명 그림", reason="그림 전체 표시", confidence=.95)
    result = framing.group_information(scenes, frames, {0: region, 1: region},
                                       dict(zoom=1.5, center=.5), dict(width=1920, height=1080))
    assert [(s["start"], s["end"]) for s in result] == [(0, .566667), (.566667, .666667)]
    assert [s["source_shot_id"] for s in result] == ["a", "b"]


def test_manual_cuts_are_reused_with_new_thumbnails(tmp_path, monkeypatch):
    source = write_video(tmp_path / "manual.avi", [solid((20, 40, 80)) for _ in range(30)])
    scenes = [dict(id="a", start=0, end=.5, time=.2, thumbnail="manual-a.jpg", frame_accurate=True),
              dict(id="b", start=.5, end=1, time=.8, thumbnail="manual-b.jpg", frame_accurate=True)]
    clip = dict(start=0, end=1, source_shots_manual=True, source_shots=scenes)
    monkeypatch.setattr(shots, "detect_shots", lambda *args: pytest.fail("manual cuts must be preserved"))
    actual, frames = framing.sample_scenes(CTX, str(source), clip, tmp_path / "frames")
    assert actual == scenes
    assert len(frames) == 2
    assert all((tmp_path / "frames" / s["thumbnail"]).is_file() for s in actual)
