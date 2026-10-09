"""Malformed AI wording must not discard independently approved candidates."""

import copy
import json

import pytest

from shortsmaker import ai


def project_and_clip():
    clip = dict(id="test-clip", start=0, end=20, title="근력 운동 한 세트의 종료 기준")
    project = dict(model="gpt-6.1-sol", effort="medium", clips=[clip],
                   transcript=[dict(start=0, end=20, text="속도와 자세 변화를 확인하세요.")])
    return project, clip


def draft(prefix="기준"):
    return dict(audience_problem="세트 종료를 고민하는 사람", content_evidence="속도와 자세 변화",
                viewer_expectation="멈출 기준", new_insight="첫 반복과 비교",
                hooks=[dict(text=f"{prefix}{i}", yellow_phrase=prefix, approach=f"표현 의도 {i}")
                       for i in range(10)])


def review(count, selected=None, round_number=1):
    selected = list(range(count - 1, -1, -1)) if selected is None else selected
    return dict(reviews=[dict(index=i, passes=i in selected,
                             reason=f"회차{round_number} 검수{i}", weakness=f"회차{round_number} 약점{i}")
                         for i in range(count)], selected_indices=selected,
                reason="첫 후보가 종료 판단의 단서를 보여줍니다.")


def review_candidates(prompt, schema, count):
    """Check the actual schema handed to the generation mock, including N=1."""
    properties = schema["properties"]
    reviews = properties["reviews"]
    selected = properties["selected_indices"]
    assert schema["additionalProperties"] is False
    assert reviews["minItems"] == reviews["maxItems"] == count
    assert reviews["items"]["properties"]["index"] == dict(type="integer", minimum=0, maximum=count - 1)
    assert selected["maxItems"] == count
    assert selected["items"] == dict(type="integer", minimum=0, maximum=count - 1)
    candidates = json.loads(prompt.split("후보 문구: ", 1)[1])
    assert [item["index"] for item in candidates] == list(range(count))
    assert all(set(item) == {"index", "text"} for item in candidates)
    return candidates


def repair_feedback(prompt):
    feedback, _ = json.JSONDecoder().raw_decode(
        prompt.split("이전 후보와 보완 지시:", 1)[1].lstrip())
    assert [item["index"] for item in feedback] == list(range(10))
    return feedback


def test_real_16_and_17_character_failures_keep_approved_hooks_and_map_indexes(tmp_path, monkeypatch):
    project, clip = project_and_clip()
    first = draft()
    # These two exact AI outputs stopped the real prepare job on 2026-10-09.
    first["hooks"][6].update(text="자세 흔들려도\n한 세트 채워?", yellow_phrase="자세 흔들려도")
    first["hooks"][8].update(text="5회째 느려진\n푸쉬업은 몇 회?", yellow_phrase="5회째 느려진")
    assert [len(first["hooks"][i]["text"]) for i in (6, 8)] == [16, 17]
    replacement = draft("변경")
    for index in (6, 8):
        replacement["hooks"][index] = copy.deepcopy(first["hooks"][index])
    valid_indexes = [0, 1, 2, 3, 4, 5, 7, 9]
    calls, review_round = [], 0

    def call(ctx, prompt, schema, *args, **kwargs):
        nonlocal review_round
        calls.append(schema)
        if schema is ai.HOOK_DRAFT_SCHEMA:
            if len(calls) == 1:
                return copy.deepcopy(first)
            feedback = repair_feedback(prompt)
            assert [item["keep"] for item in feedback] == [i in valid_indexes for i in range(10)]
            for index, length in ((6, 16), (8, 17)):
                assert str(length) in feedback[index]["feedback"]
                assert "15" in feedback[index]["feedback"]
            return copy.deepcopy(replacement)
        review_round += 1
        candidates = review_candidates(prompt, schema, 8)
        assert [item["text"] for item in candidates] == [first["hooks"][i]["text"] for i in valid_indexes]
        assert all(len(item["text"]) <= 15 for item in candidates)
        return review(8, round_number=review_round)

    monkeypatch.setattr(ai, "call", call)
    result = ai.hooks(None, project, clip, tmp_path)
    selected = list(reversed(valid_indexes))
    assert len(calls) == 4 and review_round == 2
    assert [item["text"] for item in result["hooks"]] == [first["hooks"][i]["text"] for i in selected]
    assert result["recommended_text"] == first["hooks"][9]["text"]
    assert result["review"]["selected_indices"] == selected
    decisions = {item["index"]: item for item in result["review"]["reviews"]}
    assert set(decisions) == set(range(10))
    for index in (6, 8):
        assert decisions[index]["passes"] is False
        assert decisions[index]["reason"] and decisions[index]["weakness"]
    assert decisions[7]["reason"] == "회차2 검수6"
    assert decisions[9]["reason"] == "회차2 검수7"
    assert result["hooks"][0]["evaluation"] == "회차2 검수7 · 약점: 회차2 약점7"
    assert "검수를 통과한 8개만 제안합니다." in result["reason"]


def mixed_invalid_draft():
    result = draft()
    result["hooks"][1].update(text="가" * 16, yellow_phrase="가")
    result["hooks"][2].update(text="나" * 17, yellow_phrase="나")
    result["hooks"][3].update(text="첫줄\n둘째\n셋째", yellow_phrase="첫줄")
    result["hooks"][4].update(text="강조 오류", yellow_phrase="없는 구절")
    result["hooks"][5].update(text="", yellow_phrase="")
    result["hooks"][6].update(text=" 앞뒤 공백 ", yellow_phrase="공백")
    result["hooks"][7] = copy.deepcopy(result["hooks"][0])
    result["hooks"][8].update(text="강조 누락", yellow_phrase="")
    result["hooks"][9].update(text="다" * 16, yellow_phrase="다")
    return result


def test_duplicate_invalid_highlights_and_other_format_errors_are_excluded_with_one_item_schema(tmp_path, monkeypatch):
    project, clip = project_and_clip()
    first = mixed_invalid_draft()
    calls = []

    def call(ctx, prompt, schema, *args, **kwargs):
        calls.append(schema)
        if schema is ai.HOOK_DRAFT_SCHEMA:
            if len(calls) > 1:
                feedback = repair_feedback(prompt)
                assert [item["keep"] for item in feedback] == [True] + [False] * 9
                assert all(item["feedback"] for item in feedback)
            return copy.deepcopy(first)
        candidates = review_candidates(prompt, schema, 1)
        assert candidates == [dict(index=0, text=first["hooks"][0]["text"])]
        return review(1)

    monkeypatch.setattr(ai, "call", call)
    result = ai.hooks(None, project, clip, tmp_path)
    assert len(calls) == 4
    assert [item["text"] for item in result["hooks"]] == [first["hooks"][0]["text"]]
    decisions = {item["index"]: item for item in result["review"]["reviews"]}
    assert len(decisions) == 10
    assert decisions[0]["passes"] is True
    assert all(decisions[i]["passes"] is False for i in range(1, 10))
    assert result["review"]["selected_indices"] == [0]


def test_all_format_errors_still_fail_without_independent_review(tmp_path, monkeypatch):
    project, clip = project_and_clip()
    original = copy.deepcopy(project)
    invalid = draft()
    for i, item in enumerate(invalid["hooks"]):
        item.update(text="가" * 15 + str(i), yellow_phrase="가")
    calls = []

    def call(ctx, prompt, schema, *args, **kwargs):
        calls.append(schema)
        assert schema is ai.HOOK_DRAFT_SCHEMA
        if len(calls) == 2:
            feedback = repair_feedback(prompt)
            assert not any(item["keep"] for item in feedback)
            assert all("16" in item["feedback"] for item in feedback)
        return copy.deepcopy(invalid)

    monkeypatch.setattr(ai, "call", call)
    with pytest.raises(ValueError):
        ai.hooks(None, project, clip, tmp_path)
    assert len(calls) == 2
    assert project == original


@pytest.mark.parametrize("failure", ["missing", "duplicate", "out_of_bounds"])
def test_malformed_reduced_review_is_rejected_after_repair(tmp_path, monkeypatch, failure):
    project, clip = project_and_clip()
    original = copy.deepcopy(project)
    first = draft()
    for index in (2, 7):
        first["hooks"][index].update(text="가" * 15 + str(index), yellow_phrase="가")
    calls = []

    def call(ctx, prompt, schema, *args, **kwargs):
        calls.append(schema)
        if schema is ai.HOOK_DRAFT_SCHEMA:
            return copy.deepcopy(first)
        review_candidates(prompt, schema, 8)
        malformed = review(8)
        if failure == "missing":
            malformed["reviews"].pop()
        elif failure == "duplicate":
            malformed["reviews"][-1]["index"] = 0
        else:
            malformed["selected_indices"][0] = 8
        return malformed

    monkeypatch.setattr(ai, "call", call)
    with pytest.raises(ValueError):
        ai.hooks(None, project, clip, tmp_path)
    assert len(calls) == 4
    assert project == original


def test_zero_semantic_passes_with_one_valid_format_candidate_still_fail(tmp_path, monkeypatch):
    project, clip = project_and_clip()
    calls = []

    def call(ctx, prompt, schema, *args, **kwargs):
        calls.append(schema)
        if schema is ai.HOOK_DRAFT_SCHEMA:
            return mixed_invalid_draft()
        review_candidates(prompt, schema, 1)
        return review(1, selected=[])

    monkeypatch.setattr(ai, "call", call)
    with pytest.raises(ValueError, match="통과한 후킹이 없습니다"):
        ai.hooks(None, project, clip, tmp_path)
    assert len(calls) == 4
