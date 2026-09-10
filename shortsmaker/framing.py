"""Scene cuts + multimodal important-region estimates; conservative stable crop."""

import statistics
import math
import json
import hashlib
from .jobs import Cancelled
from pathlib import Path
import cv2
from PIL import Image, ImageDraw
from . import ai


def sample_scenes(ctx, source, clip, folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise ValueError("구도 분석용 영상을 열 수 없습니다.")
    cuts = [clip["start"]]
    previous = None
    try:
        # Inspect every second for cuts. Color-distribution plus pixel changes avoid most motion cuts.
        t = clip["start"]
        while t < clip["end"]:
            ctx.check()
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                t += 1
                continue
            small = cv2.resize(frame, (160, 90))
            hist = cv2.calcHist([small], [0, 1, 2], None, [8, 8, 8], [0, 256] * 3)
            cv2.normalize(hist, hist)
            if previous is not None:
                diff = cv2.compareHist(previous[0], hist, cv2.HISTCMP_BHATTACHARYYA)
                motion = cv2.absdiff(previous[1], small).mean()
                if diff > 0.48 and motion > 35 and t - cuts[-1] >= 2:
                    cuts.append(t)
            previous = (hist, small)
            t += 1
        cuts.append(clip["end"])
        scenes = [{"start": a, "end": b} for a, b in zip(cuts, cuts[1:]) if b > a]
        frames = []
        for scene_i, scene in enumerate(scenes):
            length = scene["end"] - scene["start"]
            # Every ~12s, with both ends represented to protect movement/subtitle variation.
            count = max(3, min(16, math.ceil(length / 12) + 1))
            for j in range(count):
                t = scene["start"] + min(
                    length - 0.05, max(0.05, length * j / (count - 1))
                )
                cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
                ok, frame = cap.read()
                if not ok:
                    continue
                path = folder / f"frame-{len(frames):03}.jpg"
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                image = Image.fromarray(frame)
                image.thumbnail((960, 540))
                image.save(path, quality=88)
                frames.append(
                    dict(index=len(frames), scene=scene_i, time=t, path=str(path))
                )
        return scenes, frames
    finally:
        cap.release()


def caption_bounds(ctx, project, clip, folder):
    source = Path(__file__).resolve().parent.parent / "native/CaptionBounds.swift"
    binary = Path(folder).resolve().parents[3] / "bin/caption-bounds"
    binary.parent.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(
        json.dumps(
            [2, project["metadata"], clip["start"], clip["end"]], sort_keys=True
        ).encode()
    ).hexdigest()[:16]
    target = Path(folder) / f"captions-{key}.json"
    if target.exists():
        return json.loads(target.read_text())
    if not binary.exists() or binary.stat().st_mtime < source.stat().st_mtime:
        ctx.progress("Mac 자막 영역 인식 도구 준비 중")
        ctx.run(["swiftc", "-O", str(source), "-o", str(binary)], timeout=180)
    ctx.progress("기존 자막의 가로 범위 확인 중 · 1초 간격")
    ctx.run(
        [
            str(binary),
            project["source"],
            str(clip["start"]),
            str(clip["end"]),
            str(target),
        ],
        timeout=900,
        on_line=lambda line: (
            ctx.progress("기존 자막 범위 확인 · " + line)
            if line.startswith("OCR")
            else None
        ),
    )
    return json.loads(target.read_text())


def analyze(ctx, project, clip, folder, cache_dir):
    scenes, frames = sample_scenes(ctx, project["source"], clip, folder)
    regions = {}
    caption_error = False
    try:
        captions = caption_bounds(ctx, project, clip, folder)
    except Cancelled:
        raise
    except Exception:
        captions = []
        caption_error = True
    for offset in range(0, len(frames), 6):
        group = frames[offset : offset + 6]
        sheet = Image.new("RGB", (1920, 600 * math.ceil(len(group) / 2)), "#121212")
        draw = ImageDraw.Draw(sheet)
        for i, f in enumerate(group):
            x, y = (i % 2) * 960, (i // 2) * 600
            sheet.paste(Image.open(f["path"]), (x, y + 40))
            draw.text(
                (x + 15, y + 10), f"FRAME {f['index']} / {f['time']:.2f}s", fill="white"
            )
        sheet_path = Path(folder) / f"sheet-{offset}.jpg"
        sheet.save(sheet_path, quality=90)
        ctx.progress(f"구도 분석 · 프레임 {offset+1}~{offset+len(group)}/{len(frames)}")
        context = " ".join(
            s["text"]
            for s in project["transcript"]
            if s["end"] > clip["start"] and s["start"] < clip["end"]
        )
        prompt = f"""첨부는 원본 가로 영상의 시간별 프레임 모음입니다. 각 프레임의 전체 가로폭을 0~1로 보세요.
쇼츠 크롭에서 보존해야 할 중요 영역의 왼쪽 left, 오른쪽 right를 반환하세요.
기본 zoom은 반드시 1.5(150%)입니다. 가로 영상을 가로폭에 맞춘 뒤 150% 확대해서 좌우를 자르는 스타일입니다.
left/right는 주인물과 핵심 동작·설명에 필요한 판서/물건의 영역만 표시하세요. 배경이나 자막 전체 폭을 포함하지 마세요.
zoom은 기본 1.5를 유지하세요. 핵심 인물/동작/판서가 잘려 이해가 어려울 때만 1.0~1.5로 축소하고,
주인물이 너무 작아 핵심 표정/동작이 안 보일 때만 1.5~2.0으로 확대하세요. 배율 변경 이유를 명시하세요.
기존 자막은 참고하되 긴 자막 한 줄을 전부 보존하려고 전체 장면을 100%로 축소하지 마세요.
불확실하면 zoom=1.5와 confidence를 낮춰 사용자 확인을 요청하세요. 0<=left<right<=1. confidence는 0~1.
각 FRAME 번호마다 정확히 하나 반환하고 한국어로 이유를 간결하게 쓰세요.
FRAME 번호: {[f['index'] for f in group]}\n발언 데이터: {context}"""
        result = ai.call(
            ctx,
            prompt,
            ai.FRAME_SCHEMA,
            cache_dir,
            project["model"],
            project["effort"],
            images=[sheet_path],
        )
        expected = {f["index"] for f in group}
        if {r["index"] for r in result["frames"]} != expected or len(
            result["frames"]
        ) != len(group):
            raise ValueError(
                "AI 구도 응답의 프레임 개수가 일치하지 않습니다. 다시 시도해 주세요."
            )
        for r in result["frames"]:
            if (
                not all(math.isfinite(r[k]) for k in ("left", "right", "confidence", "zoom"))
                or not 0 <= r["left"] < r["right"] <= 1
                or not 0 <= r["confidence"] <= 1
                or not 1 <= r["zoom"] <= 2
            ):
                raise ValueError("AI 구도 좌표가 올바르지 않습니다.")
            regions[r["index"]] = r
    result = []
    for i, scene in enumerate(scenes):
        rs = [regions[f["index"]] for f in frames if f["scene"] == i]
        if not rs:
            result.append(
                dict(
                    scene,
                    zoom=1.5,
                    center=0.5,
                    review=True,
                    reason="프레임 읽기 실패 · 기본 150%, 수동 확인 필요",
                )
            )
            continue
        left = max(0, min(r["left"] for r in rs) - 0.025)
        right = min(1, max(r["right"] for r in rs) + 0.025)
        boxes = [
            b
            for f in captions
            if scene["start"] <= f["time"] < scene["end"]
            for b in f["boxes"]
        ]
        # OCR failure is surfaced, never silently presented as confident preservation.
        uncertain = caption_error or any(
            f.get("error")
            for f in captions
            if scene["start"] <= f["time"] < scene["end"]
        )
        zoom = scene_zoom(rs)
        center = (left + right) / 2
        # Subtitle width warns for review; it must not silently override 150%.
        crop_left = max(0, min(1 - 1 / zoom, center - 0.5 / zoom))
        caption_cut = any(
            max(0, b["left"]) < crop_left - 0.005
            or min(1, b["right"]) > crop_left + 1 / zoom + 0.005
            for b in boxes
        )
        result.append(
            dict(
                scene,
                zoom=round(zoom, 4),
                center=round((left + right) / 2, 4),
                review=caption_cut or uncertain or min(r["confidence"] for r in rs) < 0.8,
                reason=(
                    "자막 영역 인식 실패 · 수동 확인 필요. "
                    if uncertain
                    else "기본 150% · 핵심 내용에 필요한 경우만 배율 조정. "
                )
                + ("기존 자막 일부 잘림 가능 · 확인 필요. " if caption_cut else "")
                + rs[len(rs) // 2]["reason"],
            )
        )
    return result


def scene_zoom(regions):
    """Use 150% unless a confident semantic framing decision requires a change."""
    confident = [r["zoom"] for r in regions if r["confidence"] >= 0.8]
    reductions = [z for z in confident if z < 1.5]
    if reductions:
        return min(reductions)
    # Do not let a single close-up preference enlarge an otherwise normal scene.
    if len(confident) == len(regions) and confident and all(z > 1.5 for z in confident):
        return statistics.median(confident)
    return 1.5
