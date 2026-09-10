"""Shared 1080×1920 geometry and title raster for preview and export."""

import hashlib
import json
import math
import re
import subprocess
import unicodedata
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1080, 1920
FONT_CANDIDATES = [
    Path.home() / "Library/Fonts/GmarketSansBold.otf",
    Path("/System/Library/Fonts/AppleSDGothicNeo.ttc"),
]


def existing_path(value):
    for spelling in (
        value,
        unicodedata.normalize("NFD", value),
        unicodedata.normalize("NFC", value),
    ):
        p = Path(spelling).expanduser().resolve()
        if p.is_file():
            return p
    raise ValueError(
        "영상 파일을 찾을 수 없습니다. Finder에서 다운로드 상태와 경로를 확인해 주세요."
    )


def probe(path):
    r = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if r.returncode:
        raise ValueError(
            "영상을 읽을 수 없습니다. 파일 다운로드가 완료되었는지 확인해 주세요."
        )
    data = json.loads(r.stdout)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if not v:
        raise ValueError("영상 트랙이 없는 파일입니다.")
    w, h = v["width"], v["height"]
    rotation = next(
        (s.get("rotation", 0) for s in v.get("side_data_list", []) if "rotation" in s),
        0,
    )
    if abs(rotation) % 180 == 90:
        w, h = h, w
    duration = float(data["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("영상 길이를 확인할 수 없습니다.")
    stat = Path(path).stat()
    return dict(
        width=w,
        height=h,
        duration=duration,
        fps=v.get("avg_frame_rate", "30/1"),
        has_audio=any(s["codec_type"] == "audio" for s in data["streams"]),
        hdr=v.get("color_transfer") in ("smpte2084", "arib-std-b67"),
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )


def geometry(metadata, zoom=1.5, center=0.5, end_center=None):
    # Retain the entire source height and black title/footer space.
    max_zoom = min(2.0, 1080 * metadata["width"] / (WIDTH * metadata["height"]))
    zoom = max(1.0, min(float(zoom), max_zoom))
    sw = int(round(WIDTH * zoom / 2)) * 2
    sh = int(round(sw * metadata["height"] / metadata["width"] / 2)) * 2
    if sh > 1080:
        raise ValueError(
            "가로 영상만 지원합니다. 세로 영상은 원본 가로 롱폼을 선택해 주세요."
        )

    def offset(c):
        return math.floor(max(0.0, min(sw - WIDTH, float(c) * sw - WIDTH / 2)) / 2) * 2

    x = offset(center)
    return dict(
        zoom=sw / WIDTH,
        scaled_width=sw,
        scaled_height=sh,
        x=x,
        end_x=offset(center if end_center is None else end_center),
        y=((HEIGHT - sh) // 4) * 2,
    )


def title_image(text, yellow, path, font_size=80):
    text = str(text).strip()
    if not text or len(text) > 100:
        raise ValueError("후킹 문구는 1~100자로 입력해 주세요.")
    if yellow and yellow not in text:
        raise ValueError("노란 강조 구절은 후킹 문구에 포함되어야 합니다.")
    font_path = next((p for p in FONT_CANDIDATES if p.exists()), None)
    if not font_path:
        raise ValueError("한글 폰트가 없습니다. GmarketSansBold를 설치해 주세요.")
    # Break at whitespace closest to a balanced two-line layout; explicit newline respected.
    lines = text.splitlines()
    if len(lines) > 2:
        raise ValueError("후킹 문구는 최대 두 줄입니다.")
    if len(lines) == 1 and len(text) > 15:
        splits = [i for i, c in enumerate(text) if c == " "] or [len(text) // 2]
        at = min(splits, key=lambda i: abs(i - len(text) / 2))
        lines = [text[:at], text[at:].lstrip()]
    font_size = max(40, min(100, int(font_size)))
    while True:
        font = ImageFont.truetype(str(font_path), font_size)
        if max(font.getlength(line) for line in lines) <= 970 or font_size <= 24:
            break
        font_size -= 1
    canvas = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    # Character color lookup preserves highlights even across automatic line wrapping.
    highlight = set()
    if yellow:
        start = text.find(yellow)
        highlight = set(range(start, start + len(yellow)))
    cursor = 0
    line_height = int(font_size * 1.35)
    top = 285 - (len(lines) * line_height) / 2
    for i, line in enumerate(lines):
        start = text.find(line, cursor)
        x = (WIDTH - font.getlength(line)) / 2
        for j, char in enumerate(line):
            color = "#FFD400" if start + j in highlight else "#FFFFFF"
            draw.text(
                (x, top + i * line_height), char, font=font, fill=color, anchor="lt"
            )
            x += font.getlength(char)
        cursor = start + len(line)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path)
    return dict(lines=lines, font_size=font_size)


def render_key(project, clip):
    payload = {
        "version": 2,
        "source": project["metadata"],
        "path": project["source"],
        "clip": {
            k: v
            for k, v in clip.items()
            if k
            in (
                "start",
                "end",
                "hook",
                "yellow",
                "font_size",
                "framing",
                "manual_frame",
            )
        },
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:20]


def scenes_for(clip):
    if clip.get("manual_frame"):
        return [dict(start=clip["start"], end=clip["end"], **clip["manual_frame"])]
    scenes = clip.get("framing") or [
        dict(start=clip["start"], end=clip["end"], zoom=1.5, center=0.5)
    ]
    result, cursor = [], clip["start"]
    for s in sorted(scenes, key=lambda s: s["start"]):
        a, b = max(clip["start"], s["start"]), min(clip["end"], s["end"])
        if b <= a:
            continue
        if a > cursor + 0.001:
            result.append(dict(start=cursor, end=a, zoom=1.5, center=0.5))
        a = max(a, cursor)
        if b > a:
            result.append(dict(s, start=a, end=b))
            cursor = b
    if cursor < clip["end"] - 0.001:
        result.append(dict(start=cursor, end=clip["end"], zoom=1.5, center=0.5))
    return result


def video_filter(meta, scene, title_input="1:v"):
    g = geometry(
        meta, scene.get("zoom", 1.5), scene.get("center", 0.5), scene.get("end_center")
    )
    duration = scene["end"] - scene["start"]
    # Smoothstep pan; static when equal, resets at scene cuts.
    u = f"min(t/{duration:.6f},1)"
    x = f"{g['x']:.5f}+({g['end_x']-g['x']:.5f})*({u})*({u})*(3-2*({u}))"
    hdr = (
        (
            "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
            "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,"
        )
        if meta.get("hdr")
        else ""
    )
    return (
        f"[0:v]setpts=PTS-STARTPTS,{hdr}scale={g['scaled_width']}:{g['scaled_height']},"
        f"setsar=1,crop=1080:{g['scaled_height']}:'{x}':0,"
        f"pad=1080:1920:0:{g['y']}:black[base];"
        f"[base][{title_input}]overlay=0:0:format=auto,format=yuv420p[v]"
    )


def export_clip(ctx, project, clip, folder):
    if not clip.get("confirmed"):
        raise ValueError("후킹 문구를 확정한 뒤 인코딩해 주세요.")
    if not 0 < clip["end"] - clip["start"] <= 180.001:
        raise ValueError("쇼츠는 최대 3분입니다. 구간을 나눠 주세요.")
    key = render_key(project, clip)
    folder = Path(folder) / "renders" / key
    folder.mkdir(parents=True, exist_ok=True)
    name = (
        re.sub(r"[^\w가-힣 .-]", "", clip["hook"].replace("\n", " "))[:70].strip()
        or "쇼츠"
    )
    target = folder / (name + ".mp4")
    if target.exists():
        return dict(key=key, path=str(target), metadata=probe(target))
    title = folder / "title.png"
    title_image(clip["hook"], clip.get("yellow", ""), title, clip.get("font_size", 80))
    parts = []
    scenes = scenes_for(clip)
    for i, scene in enumerate(scenes):
        ctx.progress(f"{name} · 장면 {i+1}/{len(scenes)} 인코딩", 100 * i / len(scenes))
        part = folder / f"part-{i:03}.mp4"
        temp = folder / f"part-{i:03}.tmp.mp4"
        args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            str(scene["start"]),
            "-i",
            project["source"],
            "-loop",
            "1",
            "-i",
            str(title),
            "-t",
            str(scene["end"] - scene["start"]),
            "-filter_complex",
            video_filter(project["metadata"], scene),
            "-map",
            "[v]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "20",
            "-r",
            "30",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-af",
            "aresample=async=1:first_pts=0",
            "-movflags",
            "+faststart",
            str(temp),
        ]
        ctx.run(args, timeout=3600)
        temp.replace(part)
        parts.append(part)
    concat = folder / "concat.txt"
    concat.write_text("".join(f"file '{p.name}'\n" for p in parts))
    temp = folder / "final.tmp.mp4"
    ctx.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "1",
            "-i",
            str(concat),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(temp),
        ]
    )
    info = probe(temp)
    if abs(info["duration"] - (clip["end"] - clip["start"])) > 0.25:
        raise RuntimeError(
            "출력 길이가 구간 길이와 다릅니다. 결과를 확정하지 않았습니다."
        )
    temp.replace(target)
    for part in parts:
        part.unlink(missing_ok=True)
    return dict(key=key, path=str(target), metadata=info)
