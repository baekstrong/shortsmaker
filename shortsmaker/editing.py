"""Visual evidence for production pauses such as camera/screen adjustment."""

import math
from pathlib import Path
import cv2
from PIL import Image, ImageDraw


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
