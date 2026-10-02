"""Source views for content cuts and the opening context of hook reviews."""

import math
from pathlib import Path
import cv2
from PIL import Image, ImageDraw
from .timeline import kept_ranges, clip_duration


def hook_images(ctx, source, clip, folder):
    """Four source views from the first eight seconds of the edited timeline."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        raise ValueError("후킹 검수용 첫 화면을 읽을 수 없습니다. 원본을 확인해 주세요.")
    try:
        sheet = Image.new("RGB", (1280, 780), "#111111")
        draw = ImageDraw.Draw(sheet)
        ranges = kept_ranges(clip)
        duration = clip_duration(clip)
        if not ranges or duration < .1:
            raise ValueError("후킹 검수에 사용할 영상 구간이 없습니다.")
        for i, offset in enumerate((.2, 1.5, 3, 6)):
            ctx.check()
            elapsed = min(offset, max(0, duration - .01))
            remaining = elapsed
            for r in ranges:
                if remaining < r["end"] - r["start"]:
                    at = r["start"] + remaining
                    break
                remaining -= r["end"] - r["start"]
            cap.set(cv2.CAP_PROP_POS_MSEC, at * 1000)
            ok, frame = cap.read()
            if not ok:
                raise ValueError(f"후킹 검수용 원본 {at:.2f}초 화면을 읽을 수 없습니다.")
            thumb = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            thumb.thumbnail((640, 360))
            x, y = (i % 2) * 640, (i // 2) * 390
            sheet.paste(thumb, (x, y + 30))
            draw.text((x + 10, y + 8), f"EDITED {elapsed:.2f}s / SOURCE {at:.2f}s", fill="white")
        path = folder / "opening.jpg"
        sheet.save(path, quality=90)
        return [path]
    finally:
        cap.release()


def review_images(ctx, source, clip, folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        raise ValueError("내용 편집용 화면을 읽을 수 없습니다. 원본을 확인해 주세요.")
    images = []
    try:
        # Half-second evidence exposes brief handling between explanatory shots.
        count = math.ceil((clip["end"] - clip["start"]) / .5)
        for offset in range(0, count, 20):
            group = range(offset, min(offset + 20, count))
            sheet = Image.new("RGB", (1920, 300 * math.ceil(len(group) / 4)), "#111111")
            draw = ImageDraw.Draw(sheet)
            for i, index in enumerate(group):
                ctx.check()
                at = min(clip["end"] - .01, clip["start"] + index * .5)
                cap.set(cv2.CAP_PROP_POS_MSEC, at * 1000)
                ok, frame = cap.read()
                if not ok:
                    raise ValueError(f"원본 {at:.2f}초의 화면을 읽을 수 없습니다.")
                thumb = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                thumb.thumbnail((480, 270))
                x, y = (i % 4) * 480, (i // 4) * 300
                sheet.paste(thumb, (x, y + 30))
                draw.text((x + 10, y + 8), f"SOURCE {at:.3f}s", fill="white")
            path = folder / f"review-{offset:04}.jpg"
            sheet.save(path, quality=88)
            images.append(path)
    finally:
        cap.release()
    return images
