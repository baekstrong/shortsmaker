import json
import time

import pytest

from shortsmaker import ai, editing, framing, media, shots
from shortsmaker.jobs import Jobs
from shortsmaker.service import Service, new_clip
from shortsmaker.store import Conflict, Store


@pytest.fixture
def editor(tmp_path, monkeypatch):
    store, jobs = Store(tmp_path), Jobs(tmp_path)
    service = Service(store, jobs)
    p = store.create('/tmp/source.mp4', dict(duration=400, fps='30/1'))
    first, second = new_clip(0, 10), new_clip(60, 80)
    first.update(hook='기존 문구', yellow='기존', confirmed=True,
                 manual_frame=dict(zoom=1.2, center=.3))
    second.update(included=False, cuts=[dict(start=65, end=67, reason='기존 삭제')])
    p = store.change(p['id'], lambda p: p.update(
        clips=[first, second], transcript=[dict(start=10, end=60, text='새 구간의 실제 발언')]))
    monkeypatch.setattr(service, 'source', lambda p: None)
    calls, fail = [], {}

    def enter(stage, ctx, clip):
        calls.append((stage, clip['id']))
        if fail.get('stage') == stage:
            if fail.get('cancel'):
                ctx.cancelled.set()
                ctx.check()
            raise ValueError(stage + ' failed')

    def detect(ctx, project, clip, folder):
        enter('shots', ctx, clip)
        return [dict(id='new-shot', start=clip['start'], end=clip['end'])]

    def hooks(ctx, project, clip, *args, **kwargs):
        enter('hooks', ctx, clip)
        return dict(hooks=[dict(text='새 구간에서 확인할 것은?', yellow_phrase='확인')],
                    recommended_index=0, reason='실제 발언과 일치')

    def analyze(ctx, project, clip, *args):
        enter('framing', ctx, clip)
        return [dict(id='suggestion', start=clip['start'], end=clip['end'], zoom=1.5, center=0)]

    monkeypatch.setattr(shots, 'detect_shots', detect)
    monkeypatch.setattr(editing, 'hook_images', lambda *args: [])
    monkeypatch.setattr(ai, 'hooks', hooks)
    monkeypatch.setattr(framing, 'analyze', analyze)
    monkeypatch.setattr(service, 'analyze', lambda *args: pytest.fail('must not resplit existing shorts'))
    monkeypatch.setattr(service, 'content_edit', lambda *args: pytest.fail('must not remove content'))
    yield service, p, calls, fail
    jobs.shutdown()


def add(editor, start=10, end=60):
    service, before, *_ = editor
    p = service.edit(before['id'], dict(action='add_gap', start=start, end=end,
                                       revision=service.store.load(before['id'])['revision']))
    old_ids = {c['id'] for c in before['clips']}
    clip = next(c for c in p['clips'] if c['id'] not in old_ids)
    return p, clip


def wait_job(jobs, job):
    deadline = time.monotonic() + 5
    while jobs.items[job['id']]['status'] in ('queued', 'running'):
        assert time.monotonic() < deadline, 'job did not finish'
        time.sleep(.01)
    return jobs.items[job['id']]


def prepare(editor, clip_id):
    service, before, *_ = editor
    job = service.jobs.submit(before['id'], 'prepare_clip', dict(clip_ids=[clip_id]))
    return wait_job(service.jobs, job)


def test_add_gap_preserves_neighbors_sorts_and_is_one_undo(editor):
    service, before, *_ = editor
    added, clip = add(editor)
    assert [(c['start'], c['end']) for c in added['clips']] == [(0, 10), (10, 60), (60, 80)]
    assert [c for c in added['clips'] if c['id'] != clip['id']] == before['clips']
    assert added['history'] == [before['clips']]
    assert clip['gap_preparation'] == dict(status='pending', completed=[], resume_revision=added['revision'])
    assert clip['included'] and not clip['confirmed'] and clip['cuts'] == []
    restored = service.edit(before['id'], dict(action='undo', revision=added['revision']))
    assert restored['clips'] == before['clips']


@pytest.mark.parametrize('start,end', [
    (-1, 5), (390, 401), (390, 400.001), (10, 10), (20, 10), (10, 10.05),
    (True, 11), ('10', 20), (None, 20), (float('nan'), 20), (10, float('inf')),
    (100, 281), (9, 20), (50, 65), (62, 64), (0, 10), (1, 90),
])
def test_invalid_gap_is_atomic_including_excluded_overlap(editor, start, end):
    service, before, *_ = editor
    with pytest.raises(ValueError):
        add(editor, start, end)
    assert service.store.load(before['id']) == before


@pytest.mark.parametrize('start,end', [(80, 260), (399, 400), (10, 10.1), (100.1, 280.1)])
def test_max_length_and_exact_source_end_are_allowed(editor, start, end):
    _, clip = add(editor, start, end)
    assert (clip['start'], clip['end']) == (start, end)


def test_add_gap_rejects_stale_revision_and_busy_project(editor, monkeypatch):
    service, before, *_ = editor
    request = dict(action='add_gap', start=10, end=60)
    with pytest.raises(Conflict):
        service.edit(before['id'], dict(request, revision=0))
    monkeypatch.setattr(service.jobs, 'busy', lambda pid: True)
    with pytest.raises(ValueError):
        service.edit(before['id'], dict(request, revision=before['revision']))
    assert service.store.load(before['id']) == before


def test_new_short_prepares_only_itself_and_undo_removes_every_prepared_result(editor):
    service, before, calls, _ = editor
    _, clip = add(editor)
    job = prepare(editor, clip['id'])
    assert job['status'] == 'succeeded'
    assert '새 쇼츠' in job['message']
    assert calls == [(stage, clip['id']) for stage in ('shots', 'hooks', 'framing')]
    saved = service.store.load(before['id'])
    new = next(c for c in saved['clips'] if c['id'] == clip['id'])
    assert [c for c in saved['clips'] if c['id'] != clip['id']] == before['clips']
    assert new['hook'] and new['source_shots'] and new['frame_suggestions']
    assert not new['confirmed'] and new['frame_overrides'] == [] and new['cuts'] == []
    assert new['gap_preparation']['status'] == 'complete'
    assert job['args']['completed'] == ['shots', 'hooks', 'framing']
    assert json.loads((service.jobs.root / (job['id'] + '.json')).read_text())['args'] == job['args']
    assert saved['history'] == [before['clips']]
    restored = service.edit(before['id'], dict(action='undo', revision=saved['revision']))
    assert restored['clips'] == before['clips']


@pytest.mark.parametrize('cancel,fresh_job', [(False, False), (True, False), (False, True)])
def test_failed_or_cancelled_preparation_keeps_checkpoints_and_retries_remaining_stages(editor, cancel, fresh_job):
    service, before, calls, fail = editor
    _, clip = add(editor)
    fail.update(stage='framing', cancel=cancel)
    job = prepare(editor, clip['id'])
    assert job['status'] == ('cancelled' if cancel else 'failed')
    partial = service.store.load(before['id'])
    new = next(c for c in partial['clips'] if c['id'] == clip['id'])
    assert new['source_shots'] and new['hook']
    assert new['gap_preparation']['completed'] == ['shots', 'hooks']
    assert new['gap_preparation']['status'] == job['status']
    assert partial['history'] == [before['clips']]
    fail.clear()
    resumed = (prepare(editor, clip['id']) if fresh_job
               else wait_job(service.jobs, service.jobs.retry(job['id'])))
    assert resumed['status'] == 'succeeded'
    assert [stage for stage, _ in calls] == ['shots', 'hooks', 'framing', 'framing']
    assert service.store.load(before['id'])['history'] == [before['clips']]


@pytest.mark.parametrize('ids', [None, [], ['missing'], ['one', 'two'], 'one'])
def test_prepare_rejects_ambiguous_or_missing_targets(editor, ids):
    service, before, calls, _ = editor
    job = service.jobs.submit(before['id'], 'prepare_clip', dict(clip_ids=ids))
    assert wait_job(service.jobs, job)['status'] == 'failed'
    assert calls == [] and service.store.load(before['id']) == before


def test_prepare_cannot_overwrite_an_existing_short(editor):
    service, before, calls, _ = editor
    job = prepare(editor, before['clips'][0]['id'])
    assert job['status'] == 'failed' and '빈 구간' in job['message']
    assert calls == [] and service.store.load(before['id']) == before


def test_prepare_retries_reject_intervening_edits(editor):
    service, before, calls, fail = editor
    _, clip = add(editor)
    fail['stage'] = 'hooks'
    job = prepare(editor, clip['id'])
    edited = service.edit(before['id'], dict(action='update', clip_id=clip['id'], changes=dict(title='사용자 수정')))
    fail.clear()
    retried = wait_job(service.jobs, service.jobs.retry(job['id']))
    assert retried['status'] == 'failed' and '편집 내용이 바뀌었습니다' in retried['message']
    assert [stage for stage, _ in calls] == ['shots', 'hooks']
    assert service.store.load(before['id']) == edited


def test_repeat_completed_preparation_does_not_regenerate_or_remove_manual_edits(editor):
    service, before, calls, _ = editor
    _, clip = add(editor)
    assert prepare(editor, clip['id'])['status'] == 'succeeded'
    edited = service.edit(before['id'], dict(action='update', clip_id=clip['id'], changes=dict(hook='직접 고친 문구', yellow='')))
    assert prepare(editor, clip['id'])['status'] == 'succeeded'
    assert len(calls) == 3 and service.store.load(before['id']) == edited


def test_missing_transcript_stops_before_ai_hooks_but_keeps_cut_detection(editor):
    service, before, calls, _ = editor
    service.store.change(before['id'], lambda p: p.update(transcript=[]))
    _, clip = add(editor)
    job = prepare(editor, clip['id'])
    assert job['status'] == 'failed' and '전사 내용이 없어' in job['message']
    assert calls == [('shots', clip['id'])]
    saved = service.store.load(before['id'])
    assert next(c for c in saved['clips'] if c['id'] == clip['id'])['gap_preparation']['completed'] == ['shots']
    assert saved['history'] == [before['clips']]


def test_missing_source_keeps_new_short_and_all_existing_edits(editor, monkeypatch):
    service, before, calls, _ = editor
    _, clip = add(editor)

    def missing(project):
        raise ValueError('원본 영상 파일을 찾을 수 없습니다.')

    monkeypatch.setattr(service, 'source', missing)
    job = prepare(editor, clip['id'])
    assert job['status'] == 'failed' and '원본 영상' in job['message']
    assert calls == []
    saved = service.store.load(before['id'])
    assert [c for c in saved['clips'] if c['id'] != clip['id']] == before['clips']
    new = next(c for c in saved['clips'] if c['id'] == clip['id'])
    assert new['gap_preparation']['status'] == 'failed'
    assert saved['history'] == [before['clips']]


def test_normal_hook_and_framing_jobs_keep_their_undo_steps(editor):
    service, before, _, _ = editor
    _, clip = add(editor)
    for index, stage in enumerate(('hooks', 'framing'), 2):
        # An API argument cannot switch off the internal history flag.
        job = service.jobs.submit(before['id'], stage, dict(clip_ids=[clip['id']], _history=False))
        assert wait_job(service.jobs, job)['status'] == 'succeeded'
        assert len(service.store.load(before['id'])['history']) == index


@pytest.mark.parametrize('intervening_edit', [False, True])
def test_reconnect_resumes_missing_source_preparation_only_without_intervening_edit(editor, monkeypatch, tmp_path, intervening_edit):
    service, before, calls, _ = editor
    _, clip = add(editor)

    def missing(project):
        raise ValueError('원본 영상 파일을 찾을 수 없습니다.')

    monkeypatch.setattr(service, 'source', missing)
    failed = prepare(editor, clip['id'])
    assert failed['status'] == 'failed' and calls == []
    saved = service.store.load(before['id'])
    checkpoint_revision = next(c for c in saved['clips'] if c['id'] == clip['id'])['gap_preparation']['resume_revision']
    if intervening_edit:
        saved = service.edit(before['id'], dict(action='update', clip_id=clip['id'], changes=dict(title='직접 편집')))
    path = tmp_path / 'reconnected.mp4'
    path.write_bytes(b'same media')
    monkeypatch.setattr(media, 'probe', lambda path: dict(before['metadata'], mtime_ns=123))
    connected = service.reconnect(before['id'], path)
    state = next(c for c in connected['clips'] if c['id'] == clip['id'])['gap_preparation']
    assert state['resume_revision'] == (checkpoint_revision if intervening_edit else connected['revision'])
    assert connected['history'] == saved['history']
    monkeypatch.setattr(service, 'source', lambda p: None)
    resumed = wait_job(service.jobs, service.jobs.retry(failed['id']))
    if intervening_edit:
        assert resumed['status'] == 'failed' and '편집 내용이 바뀌었습니다' in resumed['message']
        assert calls == [] and service.store.load(before['id']) == connected
    else:
        assert resumed['status'] == 'succeeded'
        assert [stage for stage, _ in calls] == ['shots', 'hooks', 'framing']
        final = service.store.load(before['id'])
        assert final['history'] == [before['clips']]
        assert [c for c in final['clips'] if c['id'] != clip['id']] == before['clips']
