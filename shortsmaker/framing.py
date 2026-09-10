"""Find explanatory diagrams/text and apply stable interval framing."""

import hashlib
import math
from pathlib import Path
import cv2
from PIL import Image, ImageDraw
from . import ai
from .media import geometry


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
            # Sample at most 2s apart, including brief overlays and explanatory graphics.
            count = max(2, math.ceil(length / 2) + 1)
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


def analyze(ctx, project, clip, folder, cache_dir):
    scenes, frames = sample_scenes(ctx, project["source"], clip, folder)
    if not frames:
        raise ValueError("정보 확인용 프레임을 읽을 수 없습니다. 원본을 확인하고 다시 시도해 주세요.")
    regions = {}
    current = clip.get("manual_frame") or {"zoom": 1.5, "center": 0.5}
    g = geometry(project["metadata"], current["zoom"], current["center"])
    view_left = g["x"] / g["scaled_width"]
    view_right = (g["x"] + 1080) / g["scaled_width"]
    for offset in range(0, len(frames), 12):
        group = frames[offset : offset + 12]
        sheet = Image.new("RGB", (1920, 400 * math.ceil(len(group) / 3)), "#121212")
        draw = ImageDraw.Draw(sheet)
        for i, f in enumerate(group):
            x, y = (i % 3) * 640, (i // 3) * 400
            with Image.open(f["path"]) as original:
                thumb = original.copy()
                thumb.thumbnail((640, 360))
                sheet.paste(thumb, (x, y + 40))
            draw.text(
                (x + 15, y + 10), f"FRAME {f['index']} / {f['time']:.2f}s", fill="white"
            )
        sheet_path = Path(folder) / f"sheet-{offset}.jpg"
        sheet.save(sheet_path, quality=90)
        ctx.progress(f"그림·글 확인 · 프레임 {offset+1}~{offset+len(group)}/{len(frames)}")
        context = " ".join(
            s["text"]
            for s in project["transcript"]
            if s["end"] > clip["start"] and s["start"] < clip["end"]
        )
        prompt = f"""첨부는 가로 영상의 연속 프레임입니다. 각 타일에서 실제 영상 영역만 좌표0~1로 보세요.
목표는 '설명 자료가 등장하는 모든 구간'을 찾는 것입니다. 잘렸을 때만 찾는 것이 아닙니다.
각 FRAME에서 아래 중 하나가 보이면 information_present=true:
- 별도로 삽입한 그림·사진·비교 이미지·작은 설명 영상(PIP)
- 몸/팔/관절 위에 그린 색 선, 점선, 화살표, 각도 표시(초록 선 등)
- 판서·도표·슬라이드·큰 핵심 설명 글자/요점 카드
현재 크롭에서 온전히 보여도 반드시 true로 기록하세요. 위치 조정이 없어도 구간 표시는 필요합니다.
얼굴을 중앙에 놓기 위한 제안은 금지합니다. 말만 하는 인물이나 도형 없는 시범 동작만으로는 true가 아닙니다.
일반 대사 자막, 계속 붙어있는 채널 워터마크, 배경 책/간판, 단순 로고/범퍼는 제외하세요.
true이면 subject에 설명 자료 이름(예: 힙힌지 각도 초록선, 앉은 자세 비교 그림), left/right에는 그 자료 전체 폭과 작은 여백,
zoom에는 자료가 보일 권장확대율(기본1.5, 필요시1.0~2.0), confidence와 한국어 이유를 주세요.
false이면 subject/reason은 빈문자열, left=0.333,right=0.667,zoom=1.5로 간결하게 반환하세요.
현재 보이는 가로 범위 {view_left:.3f}~{view_right:.3f}. 0<=left<right<=1, confidence는0~1.
추천 위치는 해당 설명 구간에 고정 적용되고 사용자가 검토·수정합니다. 인물 이동 추적은 하지 않습니다.
FRAME 번호마다 정확히 하나: {[f['index'] for f in group]}
발언 데이터:{context}"""

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
    return group_information(scenes, frames, regions, current, project["metadata"])


def group_information(scenes, frames, regions, current, metadata):
    """Contiguous positive sample cells become real intervals, not one whole-scene hint."""
    results = []
    for scene_i, scene in enumerate(scenes):
        fs = sorted((f for f in frames if f["scene"] == scene_i), key=lambda f: f["time"])
        run = []
        def finish():
            if not run:
                return
            first, last = run[0], run[-1]
            positive = [regions[fs[i]["index"]] for i in run]
            start = scene["start"] if first == 0 else (fs[first-1]["time"] + fs[first]["time"]) / 2
            end = scene["end"] if last == len(fs)-1 else (fs[last]["time"] + fs[last+1]["time"]) / 2
            left, right = min(r["left"] for r in positive), max(r["right"] for r in positive)
            zoom = min(min(r["zoom"] for r in positive), 1 / max(right-left, .01), 2)
            zoom = max(1, zoom)
            center = (left + right) / 2
            crop = geometry(metadata, zoom, center)
            if crop["x"] <= 2:
                alignment, center = "왼쪽 정렬", 0.0
            elif crop["x"] >= crop["scaled_width"] - 1082:
                alignment, center = "오른쪽 정렬", 1.0
            elif abs(center - .5) < .04:
                alignment, center = "가운데 정렬", .5
            else:
                alignment = "왼쪽으로 위치 조정" if center < .5 else "오른쪽으로 위치 조정"
            # At 100% the horizontal alignment has no visual effect.
            if zoom <= 1.001:
                alignment, center = "전체 폭 보기", .5
            representative = max(run, key=lambda i: regions[fs[i]["index"]]["confidence"])
            f = fs[representative]; r = regions[f["index"]]
            identity = hashlib.sha256(f"{start:.3f}:{end:.3f}".encode()).hexdigest()[:16]
            results.append(dict(id=identity, start=round(start,3), end=round(end,3), time=f["time"],
                subject=r["subject"], direction=alignment, center=round(center,4), zoom=round(zoom,4),
                left=left, right=right, confidence=min(x["confidence"] for x in positive),
                reason=r["reason"], thumbnail=Path(f["path"]).name, sample_interval=2))
        for i, f in enumerate(fs):
            r = regions[f["index"]]
            if not r["information_present"]:
                finish(); run = []
                continue
            # Separate two simultaneous successive graphics on opposite sides.
            if run:
                previous = regions[fs[run[-1]]["index"]]
                if abs((r["left"]+r["right"]-previous["left"]-previous["right"])/2) > .3:
                    finish(); run = []
            run.append(i)
        finish()
    return merge_touching(results)


def merge_touching(results):
    merged = []
    for s in sorted(results, key=lambda s: s["start"]):
        if (merged and abs(merged[-1]["end"] - s["start"]) < .15
            and merged[-1].get("subject") == s.get("subject")
            and abs(merged[-1]["center"] - s["center"]) < .08
            and abs(merged[-1]["zoom"] - s["zoom"]) < .08):
            previous = merged[-1]
            previous["end"] = s["end"]
            previous["confidence"] = min(previous["confidence"], s["confidence"])
            previous["id"] = hashlib.sha256(f"{previous['start']:.3f}:{s['end']:.3f}".encode()).hexdigest()[:16]
        else:
            merged.append(dict(s))
    return merged


def apply_recommendations(clip):
    """Refresh automatic ranges while preserving every manual adjustment."""
    manual = [dict(f) for f in clip.get("frame_overrides", []) if f.get("origin") != "ai"]
    result = list(manual)
    for s in clip.get("frame_suggestions") or []:
        if not s.get("id") or "start" not in s or "end" not in s:
            continue
        start, end = max(clip["start"], s["start"]), min(clip["end"], s["end"])
        if end <= start or any(start < f["end"] and end > f["start"] for f in result):
            continue
        result.append(dict(id=s["id"], start=start, end=end, zoom=s["zoom"],
                           center=s["center"], vertical=.5, origin="ai"))
    clip["frame_overrides"] = sorted(result, key=lambda f: f["start"])
