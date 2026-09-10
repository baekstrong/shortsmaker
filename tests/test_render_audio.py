"""Actual FFmpeg regression: scene boundaries must not accumulate AAC delay."""

import subprocess
import shutil
import numpy as np
import pytest
from shortsmaker.media import probe, export_clip
from shortsmaker.service import new_clip
from shortsmaker.jobs import Jobs, Context


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_multiscene_render_retains_continuous_audio_timing(tmp_path):
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:r=30:d=5",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=sin(2*PI*440*t)*(0.5+0.45*sin(2*PI*3.7*t)):s=48000:d=5",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-shortest",
            str(source),
        ],
        check=True,
    )
    p = {"source": str(source), "metadata": probe(source)}
    c = new_clip(0.5, 3.5)
    c.update(
        hook="싱크 확인",
        yellow="싱크",
        confirmed=True,
        framing=[
            dict(start=0.5 + i * 0.5, end=1 + i * 0.5, zoom=1.5, center=0.5)
            for i in range(6)
        ],
    )
    jobs = Jobs(tmp_path / "work")
    job = {"id": "test-render", "status": "running", "created_at": "now"}
    jobs.items[job["id"]] = job
    result = export_clip(Context(jobs, job), p, c, tmp_path)

    def samples(path, start, duration):
        out = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                str(start),
                "-i",
                str(path),
                "-t",
                str(duration),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "2000",
                "-f",
                "f32le",
                "-",
            ],
            capture_output=True,
            check=True,
        )
        assert b"non monotonically" not in out.stderr
        a = np.frombuffer(out.stdout, dtype=np.float32)
        return np.sqrt(np.mean(a[: len(a) // 20 * 20].reshape(-1, 20) ** 2, axis=1))

    x = samples(source, 0.5, 3)
    y = samples(result["path"], 0, 3)
    # Late window spans five scene boundaries; old per-scene AAC accumulated >100ms.
    at, size = 150, 100
    scores = [
        np.corrcoef(x[at + lag : at + lag + size], y[at : at + size])[0, 1]
        for lag in range(-15, 16)
    ]
    assert abs(int(np.argmax(scores)) - 15) <= 1
    assert max(scores) > 0.97
