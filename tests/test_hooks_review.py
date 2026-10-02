import copy
import json
import threading
import unicodedata

import pytest

from shortsmaker import ai, editing
from shortsmaker.jobs import Cancelled, Context, Jobs
from shortsmaker.service import Service, new_clip
from shortsmaker.store import Conflict, Store
from shortsmaker.timeline import transcript_for


def hook_draft():
    return dict(
        audience_problem="draft_problem_marker",
        content_evidence="draft_evidence_marker",
        viewer_expectation="draft_expectation_marker",
        new_insight="draft_insight_marker",
        hooks=[dict(text=f"후보{i:02}", yellow_phrase="후보",
                    approach=f"draft_approach_marker_{i}") for i in range(10)],
    )


def hook_review():
    return dict(
        reviews=[dict(index=i, passes=True, reason=f"독립 검수 근거 {i}",
                      weakness=f"검수 약점 {i}") for i in range(10)],
        selected_indices=list(range(9, -1, -1)),
        reason="최우선 문구의 구체적인 시청 이유",
    )


def edited_project():
    clip = new_clip(0, 30, "발과 무릎의 관계")
    clip["cuts"] = [dict(start=0, end=10, reason="도입"),
                    dict(start=12, end=20, reason="화면 조정"),
                    dict(start=25, end=26, reason="반복")]
    project = dict(model="gpt-6.1-sol", effort="xhigh", clips=[clip],
                   transcript=[dict(start=i, end=i + 1, text=f"발언{i:02}")
                               for i in range(30)])
    return project, clip


@pytest.mark.parametrize("limit,starts", [(3, [10, 11, 20]),
                                         (8, [10, 11, 20, 21, 22, 23, 24, 26])])
def test_opening_limits_follow_edited_time_across_removed_ranges(limit, starts):
    project, clip = edited_project()
    opening = transcript_for(project, clip, limit=limit)
    assert [s["start"] for s in opening] == starts
    assert [s["text"] for s in opening] == [f"발언{i:02}" for i in starts]
    assert sum(s["end"] - s["start"] for s in opening) == limit


def test_independent_review_gets_only_candidate_text_and_original_evidence(tmp_path, monkeypatch):
    assert ai.HOOK_CANDIDATE_COUNT == 10
    draft_items = ai.HOOK_DRAFT_SCHEMA["properties"]["hooks"]
    review_items = ai.HOOK_REVIEW_SCHEMA["properties"]["reviews"]
    assert draft_items["minItems"] == draft_items["maxItems"] == 10
    assert review_items["minItems"] == review_items["maxItems"] == 10
    assert review_items["items"]["properties"]["index"]["maximum"] == 9
    assert ai.HOOK_REVIEW_SCHEMA["properties"]["selected_indices"]["items"]["maximum"] == 9
    project, clip = edited_project()
    project.update(name=unicodedata.normalize("NFD", "스쿼트 무릎 교정"),
                   summary="무릎 정렬 문제를 확인하고 맨몸 교정 운동을 시범으로 설명합니다.")
    draft, review = hook_draft(), hook_review()
    images = [tmp_path / "opening.jpg"]
    calls = []

    def call(ctx, prompt, schema, cache_dir, model, effort, **kwargs):
        calls.append(prompt)
        assert cache_dir == tmp_path
        assert (model, effort) == ("gpt-6.1-sol", "xhigh")
        assert kwargs == dict(fresh=True, images=images)
        assert "원본 주제명: 스쿼트 무릎 교정" in prompt
        assert project["name"] not in prompt
        assert "원본 흐름 요약(시청 대상·이 클립의 사용 목적을 파악하는 보조 맥락): " + project["summary"] in prompt
        assert "시청 대상을 임의로 그 운동 이름을 이미 알고 배우려는 사람으로 좁히지 마세요." in prompt
        assert "실제 첫3초 발언: 발언10 발언11 발언20" in prompt
        assert "첫8초 발언: 발언10 발언11 발언20 발언21 발언22 발언23 발언24 발언26" in prompt
        assert "발언00" not in prompt and "발언12" not in prompt and "발언25" not in prompt
        if schema is ai.HOOK_DRAFT_SCHEMA:
            return copy.deepcopy(draft)
        assert schema is ai.HOOK_REVIEW_SCHEMA
        candidates = json.loads(prompt.split("후보 문구: ", 1)[1])
        assert candidates == [dict(index=i, text=h["text"]) for i, h in enumerate(draft["hooks"])]
        assert all(set(c) == {"index", "text"} for c in candidates)
        assert all(value not in prompt for key, value in draft.items() if key != "hooks")
        assert "draft_approach_marker" not in prompt
        return copy.deepcopy(review)

    monkeypatch.setattr(ai, "call", call)
    result = ai.hooks(None, project, clip, tmp_path, fresh=True, images=images)
    assert len(calls) == 2
    assert [h["text"] for h in result["hooks"]] == [draft["hooks"][i]["text"] for i in review["selected_indices"]]
    assert result["recommended_index"] == 0
    assert result["recommended_text"] == draft["hooks"][9]["text"]
    assert result["hooks"][0]["evaluation"] == "독립 검수 근거 9 · 약점: 검수 약점 9"
    assert result["review"]["draft"] == draft


@pytest.mark.parametrize("invalid", ["failed_selection", "missing_review", "duplicate_review",
                                    "duplicate_selection", "fewer_than_ten"])
def test_invalid_independent_reviews_are_rejected(invalid):
    review = hook_review()
    if invalid == "failed_selection":
        review["reviews"][9]["passes"] = False
    elif invalid == "missing_review":
        review["reviews"].pop()
    elif invalid == "duplicate_review":
        review["reviews"][-1]["index"] = 0
    elif invalid == "duplicate_selection":
        review["selected_indices"][-1] = review["selected_indices"][0]
    else:
        review["selected_indices"].pop()
    with pytest.raises(ValueError):
        ai.validate_hook_review(review, 10)
    with pytest.raises(ValueError):
        ai.validate_hook_review(review, 10, require_ten=False)


@pytest.mark.parametrize("final_count", [10, 9, 1])
def test_weak_hooks_are_repaired_once_with_compact_feedback_and_passed_hooks_preserved(tmp_path, monkeypatch, final_count):
    project, clip = edited_project()
    calls = []
    first_draft = hook_draft()
    replacement = hook_draft()
    replacement["hooks"] = [dict(text=f"변경{i:02}", yellow_phrase="변경",
                                 approach=f"changed_approach_marker_{i}") for i in range(10)]
    bad_review = hook_review()
    bad_review["reviews"][9].update(passes=False, reason="weak_reason_marker",
                                  weakness="weak_weakness_marker")
    bad_review["selected_indices"] = list(range(8, -1, -1))

    def call(ctx, prompt, schema, *args, **kwargs):
        calls.append((schema, prompt))
        if schema is ai.HOOK_DRAFT_SCHEMA:
            if len(calls) == 3:
                feedback, _ = json.JSONDecoder().raw_decode(
                    prompt.split("이전 후보와 보완 지시:", 1)[1].lstrip())
                assert len(feedback) == 10
                assert all(set(item) == {"index", "text", "yellow_phrase", "keep", "feedback"}
                           for item in feedback)
                assert [item["index"] for item in feedback] == list(range(10))
                assert [item["keep"] for item in feedback] == [True] * 9 + [False]
                assert [item["text"] for item in feedback] == [h["text"] for h in first_draft["hooks"]]
                assert [item["yellow_phrase"] for item in feedback] == [h["yellow_phrase"] for h in first_draft["hooks"]]
                assert "weak_weakness_marker" in feedback[9]["feedback"]
                assert "draft_approach_marker" not in prompt
                assert all(value not in prompt for key, value in first_draft.items() if key != "hooks")
                assert '"reviews"' not in prompt and '"selected_indices"' not in prompt
                return copy.deepcopy(replacement)
            return copy.deepcopy(first_draft)
        if len(calls) == 4:
            candidates = json.loads(prompt.split("후보 문구: ", 1)[1])
            assert [item["text"] for item in candidates[:9]] == [h["text"] for h in first_draft["hooks"][:9]]
            assert candidates[9]["text"] == replacement["hooks"][9]["text"]
            final_review = hook_review()
            for decision in final_review["reviews"]:
                decision["passes"] = decision["index"] < final_count
            final_review["selected_indices"] = list(range(final_count - 1, -1, -1))
            return final_review
        return copy.deepcopy(bad_review)

    monkeypatch.setattr(ai, "call", call)
    result = ai.hooks(None, project, clip, tmp_path)
    merged = first_draft["hooks"][:9] + replacement["hooks"][9:]
    assert len(result["hooks"]) == final_count
    assert result["review"]["draft"]["hooks"] == merged
    assert [h["text"] for h in result["hooks"]] == [merged[i]["text"] for i in range(final_count - 1, -1, -1)]
    assert result["recommended_index"] == 0
    assert result["recommended_text"] == merged[final_count - 1]["text"]
    if final_count < 10:
        assert f"검수를 통과한 {final_count}개만 제안합니다." in result["reason"]
    assert [schema for schema, _ in calls] == [ai.HOOK_DRAFT_SCHEMA, ai.HOOK_REVIEW_SCHEMA] * 2


@pytest.mark.parametrize("failure", ["zero_passes", "malformed_review"])
def test_two_failed_reviews_raise_for_zero_passes_or_malformed_result(tmp_path, monkeypatch, failure):
    project, clip = edited_project()
    original = copy.deepcopy(project)
    calls = []
    zero_review = hook_review()
    zero_review["selected_indices"] = []
    for decision in zero_review["reviews"]:
        decision["passes"] = False

    def call(ctx, prompt, schema, *args, **kwargs):
        calls.append(schema)
        if schema is ai.HOOK_DRAFT_SCHEMA:
            if len(calls) == 3:
                feedback, _ = json.JSONDecoder().raw_decode(
                    prompt.split("이전 후보와 보완 지시:", 1)[1].lstrip())
                assert len(feedback) == 10 and not any(item["keep"] for item in feedback)
            return hook_draft()
        review = copy.deepcopy(zero_review)
        if len(calls) == 4 and failure == "malformed_review":
            review["reviews"].pop()
        return review

    monkeypatch.setattr(ai, "call", call)
    expected = "통과한 후킹이 없습니다" if failure == "zero_passes" else "누락 또는 중복"
    with pytest.raises(ValueError, match=expected):
        ai.hooks(None, project, clip, tmp_path)
    assert calls == [ai.HOOK_DRAFT_SCHEMA, ai.HOOK_REVIEW_SCHEMA] * 2
    assert project == original


@pytest.mark.parametrize("interruption", ["failure", "cancel", "conflict"])
def test_multi_clip_hook_interruption_never_saves_partial_results(tmp_path, monkeypatch, interruption):
    store, jobs = Store(tmp_path), Jobs(tmp_path)
    service = Service(store, jobs)
    project = store.create(tmp_path / "source.mp4", dict(duration=40))
    clips = [new_clip(0, 20), new_clip(20, 40)]
    for clip in clips:
        clip.update(hook="기존 문구", yellow="기존", confirmed=True,
                    manual_frame=dict(zoom=1.2, center=.4))
    project = store.change(project["id"], lambda p: p.update(clips=clips))
    monkeypatch.setattr(service, "source", lambda p: None)
    monkeypatch.setattr(editing, "hook_images", lambda ctx, source, clip, folder: [clip["id"] + ".jpg"])
    first_collected = threading.Event()
    expected = [project]
    ctx = Context(jobs, dict(id="isolated-hook-test"))

    def progress(message, percent=None):
        ctx.check()
        if "1/2 완료" in message:
            if interruption == "cancel":
                ctx.cancelled.set()
            elif interruption == "conflict":
                expected[0] = store.change(project["id"], lambda p: p.update(name="동시 사용자 수정"))
            first_collected.set()

    monkeypatch.setattr(ctx, "progress", progress)

    def hooks(context, p, clip, cache_dir, fresh, *, images):
        assert fresh and images == [clip["id"] + ".jpg"]
        if clip["id"] == clips[1]["id"]:
            assert first_collected.wait(3), "첫 결과가 수집되지 않았습니다."
            if interruption == "failure":
                raise ValueError("두 번째 클립 검수 실패")
            context.check()
        return dict(hooks=[dict(text="새 후킹", yellow_phrase="후킹")],
                    recommended_index=0, reason="독립 선별 근거")

    monkeypatch.setattr(ai, "hooks", hooks)
    exception = {"failure": ValueError, "cancel": Cancelled, "conflict": Conflict}[interruption]
    try:
        with pytest.raises(exception):
            service.hooks(ctx, project["id"], dict(fresh=True, replace_selected=True))
        assert first_collected.is_set()
        assert store.load(project["id"]) == expected[0]
        assert store.load(project["id"])["clips"] == clips
        assert store.load(project["id"])["history"] == []
    finally:
        jobs.shutdown()


def test_hook_images_seek_edited_samples_across_cuts_and_release_capture(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import numpy as np
    from PIL import Image

    clip = new_clip(0, 30)
    clip["cuts"] = [dict(start=0, end=10, reason="도입"),
                    dict(start=11, end=20, reason="화면 조정"),
                    dict(start=22, end=25, reason="반복")]
    source = tmp_path / "source.mp4"
    events, checks = [], []

    class Capture:
        reads = 0

        def isOpened(self):
            return True

        def set(self, prop, value):
            events.append(("seek", prop, value))

        def read(self):
            self.reads += 1
            events.append(("read",))
            return True, np.full((32, 64, 3), self.reads * 40, dtype=np.uint8)

        def release(self):
            events.append(("release",))

    capture = Capture()

    def open_capture(path):
        assert path == str(source)
        return capture

    monkeypatch.setattr(editing.cv2, "VideoCapture", open_capture)
    ctx = SimpleNamespace(check=lambda: checks.append(True))
    paths = editing.hook_images(ctx, source, clip, tmp_path / "frames")
    seeks = [(prop, value) for kind, *details in events if kind == "seek"
             for prop, value in [details]]
    assert [prop for prop, _ in seeks] == [editing.cv2.CAP_PROP_POS_MSEC] * 4
    assert [value for _, value in seeks] == pytest.approx([10200, 20500, 25000, 28000])
    assert capture.reads == len(checks) == 4
    assert events[-1] == ("release",)
    assert paths == [tmp_path / "frames" / "opening.jpg"]
    with Image.open(paths[0]) as sheet:
        assert sheet.format == "JPEG" and sheet.size == (1280, 780)
        for i in range(4):
            pixel = sheet.getpixel(((i % 2) * 640 + 20, (i // 2) * 390 + 45))
            assert pixel == pytest.approx((40 * (i + 1),) * 3, abs=5)
