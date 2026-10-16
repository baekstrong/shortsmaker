"""Actual frame markers verify shot boundaries after 30 fps export."""

import json
import math
import shutil
import subprocess
from fractions import Fraction

import numpy as np
import pytest

from shortsmaker.media import export_clip, probe
from shortsmaker.service import new_clip


class RenderContext:
    def progress(self, *args):
        pass

    def run(self, args, **kwargs):
        return subprocess.run(args, capture_output=True, text=True, check=True).stdout


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
@pytest.mark.parametrize("rate", ["30000/1001", "30", "60"])
def test_each_shot_starts_with_its_source_frame_and_keeps_audio_length(tmp_path, rate):
    fps = float(Fraction(rate))
    frame_count, cut_frame = 18, 7
    duration, boundary = frame_count / fps, cut_frame / fps
    source = tmp_path / "source.mp4"
    # Red identifies the original frame; green identifies the crop's horizontal
    # position. This catches both source-frame leakage and late framing changes.
    frames = np.zeros((frame_count, 180, 320, 3), dtype=np.uint8)
    frames[:, :, :, 0] = (30 + np.arange(frame_count) * 8)[:, None, None]
    frames[:, :, :, 1] = np.arange(320)[None, None, :] * 200 / 320 + 10
    frames[:, :, :, 2] = 80
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pixel_format", "rgb24",
         "-video_size", "320x180", "-framerate", rate, "-i", "pipe:0",
         "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration}",
         "-c:v", "libx264rgb", "-qp", "0", "-c:a", "aac", "-t", str(duration), str(source)],
        input=frames.tobytes(), capture_output=True, check=True,
    )
    project = dict(source=str(source), metadata=probe(source))
    clip = new_clip(0, duration)
    clip.update(
        hook="컷 경계 확인", confirmed=True,
        manual_frame=dict(zoom=1.5, center=.5),
        frame_overrides=[dict(id="after", start=boundary, end=duration, zoom=1.5, center=.2)],
    )
    result = export_clip(RenderContext(), project, clip, tmp_path)
    decoded = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", result["path"], "-vf",
         "crop=2:2:540:960,format=rgb24", "-f", "rawvideo", "-"],
        capture_output=True, check=True,
    )
    pixels = np.frombuffer(decoded.stdout, dtype=np.uint8).reshape(-1, 2, 2, 3).mean(axis=(1, 2))
    source_frames = np.rint((pixels[:, 0] - 30) / 8).astype(int)
    output_cut = round(boundary * 30)
    assert len(pixels) == round(duration * 30)
    assert source_frames[0] == 0
    assert source_frames[output_cut] == cut_frame
    assert np.all(source_frames[:output_cut] < cut_frame)
    assert np.all(source_frames[output_cut:] >= cut_frame)
    assert np.all(pixels[:output_cut, 1] > 95)
    assert np.all(pixels[output_cut:, 1] < 90)
    # The old output -r conversion started each 60 fps shot with consecutive
    # source frames, leaving its last frame three source frames behind.
    for first, start, end in [(0, 0, output_cut), (cut_frame, output_cut, len(pixels))]:
        for offset, actual in enumerate(source_frames[start:end]):
            expected = first + math.floor(offset * fps / 30 + 1e-7)
            assert abs(actual - expected) <= 1
    streams = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", result["path"]],
        capture_output=True, text=True, check=True,
    ).stdout)["streams"]
    video = next(s for s in streams if s["codec_type"] == "video")
    audio = next(s for s in streams if s["codec_type"] == "audio")
    assert Fraction(video["r_frame_rate"]) == 30
    assert abs(float(video["duration"]) - duration) <= 1 / 30
    assert abs(float(audio["duration"]) - duration) < .005
