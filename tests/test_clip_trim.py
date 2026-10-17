import pytest
from types import SimpleNamespace

from shortsmaker.jobs import Jobs
from shortsmaker.media import render_key
from shortsmaker.service import Service, new_clip
from shortsmaker.store import Conflict, Store


@pytest.fixture
def editor(tmp_path):
    store, jobs = Store(tmp_path), Jobs(tmp_path)
    project = store.create('/tmp/source.mp4', dict(duration=10, fps='30/1'))
    clip = new_clip(0, 3, '기존 쇼츠')
    clip.update(
        hook='기존 후킹', yellow='기존', confirmed=True,
        manual_frame=dict(zoom=1.2, center=.3),
        frame_overrides=[dict(id='left', start=0, end=.5, zoom=1.5, center=0),
                         dict(id='right', start=2, end=3, zoom=1.5, center=1)],
        frame_suggestions=[dict(id='suggestion', start=2, end=3)],
        cuts=[dict(start=.2, end=.4, reason='기존 삭제'),
              dict(start=2.5, end=2.8, reason='기존 삭제')],
        source_shots_manual=True,
        source_shots=[dict(id='a', start=0, end=1.041, time=.5, thumbnail='a.jpg',
                           boundary_kind='manual', frame_times=[i / 30 for i in range(32)]),
                      dict(id='b', start=1.041, end=2, time=1.5, thumbnail='b.jpg',
                           boundary_kind='detected', frame_times=[1.041, 1.5, 1.9]),
                      dict(id='c', start=2, end=3, time=2.5, thumbnail='c.jpg',
                           frame_times=[2, 2.1, 2.3, 2.5, 2.8])],
        content_edit_version=1,
    )
    project = store.change(project['id'], lambda p: p.update(clips=[clip]))
    yield Service(store, jobs), project
    jobs.shutdown()


def update(editor, **changes):
    service, project = editor
    return service.edit(project['id'], dict(
        action='update', clip_id=project['clips'][0]['id'],
        changes=changes, revision=service.store.load(project['id'])['revision']))


def test_shortening_keeps_manual_cuts_framing_and_one_step_undo(editor):
    service, before = editor
    old = before['clips'][0]
    edited = update(editor, start=.12, end=2.4)
    clip = edited['clips'][0]
    assert edited['revision'] == before['revision'] + 1
    assert edited['history'] == [before['clips']]
    assert [(s['id'], s['start'], s['end']) for s in clip['source_shots']] == [
        ('a', .12, 1.041), ('b', 1.041, 2), ('c', 2, 2.4)]
    assert clip['source_shots_manual']
    assert clip['source_shots'][0]['frame_times'][0] == .1
    assert clip['source_shots'][1]['frame_times'][0] == 1.041
    assert clip['source_shots'][1]['boundary_kind'] == 'detected'
    assert all(s['start'] <= s['time'] < s['end'] for s in clip['source_shots'])
    assert clip['source_shots'][-1]['time'] != old['source_shots'][-1]['time']
    assert clip['source_shots'][-1]['thumbnail'] != old['source_shots'][-1]['thumbnail']
    assert [(f['start'], f['end']) for f in clip['frame_overrides']] == [(.12, .5), (2, 2.4)]
    assert clip['frame_suggestions'] == [dict(id='suggestion', start=2, end=2.4)]
    assert clip['cuts'] == [old['cuts'][0]]
    for key in ('hook', 'yellow', 'title', 'manual_frame', 'included'):
        assert clip[key] == old[key]
    assert not clip['confirmed'] and 'content_edit_version' not in clip
    assert render_key(edited, clip) != render_key(before, old)
    restored = service.edit(before['id'], dict(action='undo', revision=edited['revision']))
    assert restored['clips'] == before['clips']


def test_expanding_requires_new_cut_coverage_without_losing_framing(editor):
    update(editor, start=.12, end=2.4)
    result = update(editor, end=3)['clips'][0]
    assert 'source_shots' not in result and 'source_shots_manual' not in result
    assert [(f['start'], f['end']) for f in result['frame_overrides']] == [(.12, .5), (2, 2.4)]
    assert result['hook'] == '기존 후킹'


def test_unrelated_update_preserves_cut_metadata(editor):
    _, before = editor
    result = update(editor, title='새 이름')['clips'][0]
    assert result['source_shots'] == before['clips'][0]['source_shots']
    assert result['source_shots_manual'] and result['confirmed']


def test_trim_at_existing_boundary_drops_earlier_shot_and_its_last_frame(editor):
    result = update(editor, start=1.041, end=2.4)['clips'][0]
    assert [s['id'] for s in result['source_shots']] == ['b', 'c']
    assert result['source_shots'][0]['frame_times'][0] == 1.041
    assert result['cuts'] == []


def test_split_keeps_manual_cut_boundaries_in_both_halves(editor):
    service, before = editor
    result = service.edit(before['id'], dict(
        action='split', clip_id=before['clips'][0]['id'], at=1.6, revision=before['revision']))
    left, right = result['clips']
    assert [(s['start'], s['end']) for s in left['source_shots']] == [(0, 1.041), (1.041, 1.6)]
    assert [(s['start'], s['end']) for s in right['source_shots']] == [(1.6, 2), (2, 3)]
    assert left['source_shots_manual'] and right['source_shots_manual']
    assert right['source_shots'][0]['frame_times'][0] == 1.5


@pytest.mark.parametrize('changes', [
    dict(start=-.1), dict(end=11), dict(start=3), dict(start=4),
    dict(start=True), dict(end='2'), dict(start=None),
    dict(start=float('nan')), dict(end=float('inf')),
    dict(start=1, end=1.05), dict(start=.2, end=.4),
])
def test_invalid_trim_does_not_change_data_or_undo_history(editor, changes):
    service, before = editor
    with pytest.raises(ValueError):
        update(editor, **changes)
    assert service.store.load(before['id']) == before


def test_busy_and_stale_trim_are_rejected(editor, monkeypatch):
    service, before = editor
    request = dict(action='update', clip_id=before['clips'][0]['id'], changes=dict(end=2.4))
    with pytest.raises(Conflict):
        service.edit(before['id'], dict(request, revision=before['revision'] - 1))
    monkeypatch.setattr(service.jobs, 'busy', lambda pid: True)
    with pytest.raises(ValueError):
        service.edit(before['id'], dict(request, revision=before['revision']))
    assert service.store.load(before['id']) == before


@pytest.mark.parametrize('explicit', [True, False])
def test_shot_detection_can_edit_explicit_excluded_short_but_batch_skips_it(editor, monkeypatch, explicit):
    from shortsmaker import shots

    service, project = editor
    excluded = project['clips'][0]
    excluded['included'] = False
    excluded.pop('source_shots')
    excluded.pop('source_shots_manual')
    included = new_clip(4, 5)
    service.store.change(project['id'], lambda p: p.update(clips=[excluded, included]))
    monkeypatch.setattr(service, 'source', lambda p: None)
    detected = []

    def detect(ctx, project, clip, folder):
        detected.append(clip['id'])
        return [dict(id='found', start=clip['start'], end=clip['end'])]

    monkeypatch.setattr(shots, 'detect_shots', detect)
    args = dict(clip_ids=[excluded['id']]) if explicit else {}
    service.shots(SimpleNamespace(progress=lambda *args: None, check=lambda: None), project['id'], args)
    saved = service.store.load(project['id'])
    assert detected == [excluded['id'] if explicit else included['id']]
    assert not saved['clips'][0]['included']
    assert ('source_shots' in saved['clips'][0]) is explicit
    assert ('source_shots' in saved['clips'][1]) is not explicit
    # All other batch stages retain the existing exclusion policy.
    with pytest.raises(ValueError, match='작업할 쇼츠'):
        service.selected(saved, dict(clip_ids=[excluded['id']]))


@pytest.mark.parametrize('automatic', [True, False])
def test_automatic_cut_detection_does_not_add_an_undo_step_after_trim(editor, monkeypatch, automatic):
    from shortsmaker import shots

    service, before = editor
    expanded = update(editor, end=4)
    assert 'source_shots' not in expanded['clips'][0]
    monkeypatch.setattr(service, 'source', lambda p: None)
    monkeypatch.setattr(shots, 'detect_shots', lambda ctx, p, clip, folder: [
        dict(id='fresh', start=clip['start'], end=clip['end'])])
    args = dict(clip_ids=[before['clips'][0]['id']])
    if automatic:
        args['automatic'] = True
    service.shots(SimpleNamespace(progress=lambda *args: None, check=lambda: None), before['id'], args)
    detected = service.store.load(before['id'])
    assert detected['clips'][0]['source_shots'][0]['end'] == 4
    assert len(detected['history']) == (1 if automatic else 2)
    restored = service.edit(before['id'], dict(action='undo', revision=detected['revision']))
    assert restored['clips'] == (before if automatic else expanded)['clips']
