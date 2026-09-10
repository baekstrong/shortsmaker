"""Advisory checks for cropped diagrams/text; never apply subject tracking."""

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


def analyze(ctx, project, clip, folder, cache_dir):
    scenes, frames = sample_scenes(ctx, project["source"], clip, folder)
    if not frames:
        raise ValueError("정보 확인용 프레임을 읽을 수 없습니다. 원본을 확인하고 다시 시도해 주세요.")
    regions = {}
    current = clip.get("manual_frame") or {"zoom": 1.5, "center": 0.5}
    g = geometry(project["metadata"], current["zoom"], current["center"])
    view_left = g["x"] / g["scaled_width"]
    view_right = (g["x"] + 1080) / g["scaled_width"]
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
        ctx.progress(f"그림·글 확인 · 프레임 {offset+1}~{offset+len(group)}/{len(frames)}")
        context = " ".join(
            s["text"]
            for s in project["transcript"]
            if s["end"] > clip["start"] and s["start"] < clip["end"]
        )
        prompt = f"""첨부 이미지의 각 FRAME은 원본 가로 영상입니다. 한국어로 답하세요.
현재 쇼츠는 기본150%·가운데 고정이며 사용자가 직접 조정합니다. AI는 위치를 바꾸지 않고 제안만 합니다.
현재 보이는 원본 가로 범위는 {view_left:.3f}~{view_right:.3f}입니다(전체폭0~1).
각 FRAME에서 설명에 필요한 그림·도표·슬라이드·판서·정보성 글이 현재 범위 밖으로 잘려 이해가 어려운 경우만 information_cut=true.
사람 얼굴·몸·손을 따라가거나 가운데에 맞추려고 제안하지 마세요. 얼굴이 가장자리에 있거나 잘려도 그것만으로 제안하지 마세요.
기존 대사 자막의 긴 줄, 배경 책 표지·장식 글자도 제안 대상이 아닙니다. 발언 내용상 실제로 보여주는 정보여야 합니다.
information_cut=true이면 해당 정보의 left/right와 보존할 권장zoom(1.0~2.0, 기본1.5), confidence, 한국어 이유를 반환하세요.
필요 없으면 information_cut=false, left=0.333, right=0.667, zoom=1.5로 반환하세요.
불확실한 정보는 confidence를 낮추고 이유에 확인 필요를 적으세요. 0<=left<right<=1, confidence는0~1.
각 FRAME 번호마다 정확히 하나 반환하세요. 번호:{[f['index'] for f in group]}
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
    result = []
    for i, scene in enumerate(scenes):
        rs = [(f, regions[f["index"]]) for f in frames
              if f["scene"] == i and regions[f["index"]]["information_cut"]]
        if not rs:
            continue
        # One advisory card per scene, with a sampled moment to inspect.
        f, r = max(rs, key=lambda pair: pair[1]["confidence"])
        center = (r["left"] + r["right"]) / 2
        direction = "왼쪽 정보 확인" if center < (view_left + view_right) / 2 - 0.05 else (
            "오른쪽 정보 확인" if center > (view_left + view_right) / 2 + 0.05 else "확대율 확인")
        result.append(dict(scene, time=f["time"], center=round(center, 4),
                           zoom=r["zoom"], confidence=r["confidence"],
                           direction=direction, reason=r["reason"]))
    return result
