"""Read-only whole-project media audit; writes a compact report under data/verification."""

import json
import subprocess
from pathlib import Path
import numpy as np
from shortsmaker.store import Store
from shortsmaker.media import geometry, render_key


def envelope(path, start, duration):
    result = subprocess.run(
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
    assert b"non monotonically" not in result.stderr, result.stderr.decode()
    a = np.frombuffer(result.stdout, dtype=np.float32)
    return np.sqrt(np.mean(a[: len(a) // 20 * 20].reshape(-1, 20) ** 2, axis=1))


def main():
    store = Store("data")
    report = {"projects": []}
    for p in store.list():
        assert Path(p["source"]).stat().st_size == p["metadata"]["size"]
        assert Path(p["source"]).stat().st_mtime_ns == p["metadata"]["mtime_ns"]
        result = {
            "project_id": p["id"],
            "name": p["name"],
            "original_unchanged": True,
            "clips": [],
        }
        for i, c in enumerate(p["clips"]):
            assert len(c["hooks"]) == 10 and 0 <= c["recommended_index"] <= 9
            assert all(h["yellow_phrase"] in h["text"] for h in c["hooks"])
            assert c["confirmed"] and c["framing"]
            r = c["render"]
            assert r["key"] == render_key(p, c)
            raw = json.loads(
                subprocess.check_output(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_streams",
                        "-show_format",
                        "-of",
                        "json",
                        r["path"],
                    ]
                )
            )
            v = next(s for s in raw["streams"] if s["codec_type"] == "video")
            a = next(s for s in raw["streams"] if s["codec_type"] == "audio")
            duration = c["end"] - c["start"]
            actual = float(raw["format"]["duration"])
            assert (v["width"], v["height"], v["codec_name"], a["codec_name"]) == (
                1080,
                1920,
                "h264",
                "aac",
            )
            assert duration <= 180.001 and abs(actual - duration) < 0.1
            x = envelope(p["source"], c["start"], duration)
            y = envelope(r["path"], 0, duration)
            windows = []
            for t in (3, duration / 2, duration - 12):
                at = int(t * 100)
                count = min(700, len(y) - at - 31)
                yw = y[at : at + count]
                scores = [
                    float(np.corrcoef(x[at + lag : at + lag + len(yw)], yw)[0, 1])
                    for lag in range(-20, 21)
                ]
                best = int(np.argmax(scores))
                offset = (best - 20) / 100
                assert abs(offset) <= 0.02 and scores[best] > 0.95, (
                    p["name"],
                    i + 1,
                    t,
                    offset,
                    scores[best],
                )
                windows.append(
                    {
                        "at": round(t, 2),
                        "offset_seconds": offset,
                        "correlation": round(scores[best], 6),
                    }
                )
            boxes_checked = 0
            for file in (store.folder(p["id"]) / "frames" / c["id"]).glob(
                "captions-*.json"
            ):
                for frame in json.loads(file.read_text()):
                    scene = next(
                        (
                            f
                            for f in c["framing"]
                            if f["start"] <= frame["time"] < f["end"]
                        ),
                        None,
                    )
                    if not scene:
                        continue
                    g = geometry(p["metadata"], scene["zoom"], scene["center"])
                    left = g["x"] / g["scaled_width"]
                    right = (g["x"] + 1080) / g["scaled_width"]
                    for box in frame["boxes"]:
                        assert (
                            max(0, box["left"]) >= left - 0.002
                            and min(1, box["right"]) <= right + 0.002
                        ), (p["name"], i + 1, frame["time"])
                        boxes_checked += 1
            dest = store.root / "verification" / f"{p['id'][:8]}-{i+1:02}.png"
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-ss",
                    str(duration / 2),
                    "-i",
                    r["path"],
                    "-frames:v",
                    "1",
                    str(dest),
                ],
                check=True,
            )
            result["clips"].append(
                {
                    "index": i + 1,
                    "title": c["hook"],
                    "render_key": r["key"],
                    "duration": actual,
                    "expected_duration": round(duration, 3),
                    "video_codec": v["codec_name"],
                    "audio_codec": a["codec_name"],
                    "width": v["width"],
                    "height": v["height"],
                    "audio_windows": windows,
                    "caption_boxes_checked": boxes_checked,
                    "outside_caption_boxes": 0,
                    "review_scenes": sum(bool(s["review"]) for s in c["framing"]),
                }
            )
        report["projects"].append(result)
        print(p["name"], len(result["clips"]), "개 검증 통과", flush=True)
    path = store.root / "verification/final-output-audit.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(path)


if __name__ == "__main__":
    main()
