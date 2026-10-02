"""Keep source timecodes for edits; derive the shortened playback timeline."""

import math


def validate_cuts(clip):
    last = clip["start"]
    cuts = clip.get("cuts", [])
    if not isinstance(cuts, list):
        raise ValueError("삭제 구간 목록이 올바르지 않습니다.")
    for cut in cuts:
        if not isinstance(cut, dict):
            raise ValueError("삭제 구간이 올바르지 않습니다.")
        a, b = cut.get("start"), cut.get("end")
        if (not all(isinstance(t, (int, float)) and not isinstance(t, bool)
                    and math.isfinite(t) for t in (a, b))
                or not last <= a < b <= clip["end"]
                or not isinstance(cut.get("reason"), str) or not cut["reason"].strip()):
            raise ValueError("삭제 구간의 시간·순서·이유를 확인해 주세요.")
        last = b
    if clip_duration(clip) < .1:
        raise ValueError("쇼츠 전체를 삭제할 수 없습니다. 남길 내용을 확인해 주세요.")


def kept_ranges(clip):
    result, cursor = [], clip["start"]
    for cut in clip.get("cuts", []):
        if cut["start"] > cursor:
            result.append(dict(start=cursor, end=cut["start"]))
        cursor = cut["end"]
    if cursor < clip["end"]:
        result.append(dict(start=cursor, end=clip["end"]))
    return result


def clip_duration(clip):
    return sum(r["end"] - r["start"] for r in kept_ranges(clip))


def trim_cuts(clip):
    clip["cuts"] = [dict(c, start=max(clip["start"], c["start"]),
                         end=min(clip["end"], c["end"]))
                    for c in clip.get("cuts", [])
                    if c["end"] > clip["start"] and c["start"] < clip["end"]]


def transcript_for(project, clip, limit=None):
    result = []
    remaining = limit
    for r in kept_ranges(clip):
        if remaining is not None:
            if remaining <= 0:
                break
            end = min(r["end"], r["start"] + remaining)
            remaining -= end - r["start"]
            r = dict(r, end=end)
        for s in project["transcript"]:
            if s["end"] <= r["start"] or s["start"] >= r["end"]:
                continue
            words = [w for w in s.get("words", [])
                     if w["end"] > r["start"] and w["start"] < r["end"]]
            text = " ".join(w["text"] for w in words) if s.get("words") else s["text"]
            if text.strip():
                result.append(dict(start=max(s["start"], r["start"]),
                                   end=min(s["end"], r["end"]), text=text))
    return result
