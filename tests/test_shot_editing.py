import copy
import pytest
from shortsmaker.jobs import Jobs
from shortsmaker.media import render_key, scenes_for
from shortsmaker.service import Service, new_clip
from shortsmaker.store import Store, Conflict


@pytest.fixture
def editor(tmp_path):
    store = Store(tmp_path)
    p = store.create('/tmp/source.mp4', dict(duration=10, width=1920, height=1080, fps='30/1'))
    clip = new_clip(0, 3)
    clip.update(hook='기존 문구', yellow='기존', source_shots=[
        dict(id=f's{i}', start=i, end=i+1, time=i+.5, thumbnail=f's{i}.jpg',
             frame_times=[i+j/30 for j in range(30)]) for i in range(3)])
    p = store.change(p['id'], lambda p: p.update(clips=[clip]))
    return Service(store, Jobs(tmp_path)), p


def edit(editor, action, **kwargs):
    service, p = editor
    return service.edit(p['id'], dict(action=action, clip_id=p['clips'][0]['id'], **kwargs))


def test_apply_one_shot_replaces_overlap_and_preserves_neighbors_undo(editor):
    service, p = editor
    c = p['clips'][0]
    c['frame_overrides'] = [dict(id='old', start=.2, end=2.8, zoom=1.2, center=1, origin='manual')]
    service.store.change(p['id'], lambda p: p.update(clips=[c]))
    before = render_key(p, c)
    p = edit(editor, 'frame_shot', shot_id='s1', frame=dict(zoom=1.5, center=0))
    result = p['clips'][0]
    assert [(f['start'], f['end'], f['center']) for f in result['frame_overrides']] == [
        (.2, 1, 1), (1, 2, 0), (2, 2.8, 1)]
    assert len({f['id'] for f in result['frame_overrides']}) == 3
    assert result['hook'] == c['hook'] and result['cuts'] == []
    assert render_key(p, result) != before
    restored = edit(editor, 'undo')['clips'][0]
    assert restored == c


def test_reset_shot_clears_only_that_shot(editor):
    edit(editor, 'frame_region_add', start=0, end=3)
    result = edit(editor, 'frame_shot', shot_id='s1', frame=None)['clips'][0]
    assert [(f['start'], f['end']) for f in result['frame_overrides']] == [(0, 1), (2, 3)]


def test_reapply_shot_after_moving_its_region_preserves_both_ranges(editor):
    edit(editor, 'frame_shot', shot_id='s0', frame=dict(zoom=1.2, center=0))
    moved = edit(editor, 'frame_region_time', suggestion_id='s0', start=1.2, end=1.8)['clips'][0]
    result = edit(editor, 'frame_shot', shot_id='s0', frame=dict(zoom=1.5, center=1))['clips'][0]
    assert [(f['start'], f['end'], f['zoom'], f['center']) for f in result['frame_overrides']] == [
        (0, 1, 1.5, 1), (1.2, 1.8, 1.2, 0)]
    assert len({f['id'] for f in result['frame_overrides']}) == 2
    assert result['frame_overrides'][0]['id'] == 's0'
    assert edit(editor, 'undo')['clips'][0] == moved


def test_cut_edits_preserve_pts_of_frame_visible_at_fractional_clip_start(editor):
    service, p = editor
    c = copy.deepcopy(p['clips'][0])
    c['start'] = c['source_shots'][0]['start'] = .12
    c['source_shots'][0]['frame_times'] = [t for t in c['source_shots'][0]['frame_times'] if t >= .1]
    expected = sorted(t for s in c['source_shots'] for t in s['frame_times'])
    service.store.change(p['id'], lambda p: p.update(clips=[c]))
    for action, fields in [
        ('shot_split', dict(at=.5)),
        ('shot_boundary', dict(shot_id='s1', edge='start', at=1.04)),
        ('shot_merge', dict(shot_id='s0')),
    ]:
        result = edit(editor, action, **fields)['clips'][0]
        assert result['source_shots'][0]['frame_times'][0] == .1
        assert sorted(t for s in result['source_shots'] for t in s['frame_times']) == expected
        assert result['start'] == .12
        assert all(s['start'] <= s['time'] < s['end'] for s in result['source_shots'])


def test_boundary_uses_actual_pts_and_moves_shared_applied_boundary(editor):
    service, p = editor
    c = copy.deepcopy(p['clips'][0])
    # A VFR timestamp differs from a 30fps grid. Both sides must use the actual frame.
    c['source_shots'][1]['frame_times'][1] = 1.041
    c['frame_suggestions'] = [dict(id='suggestion', start=1, end=2)]
    service.store.change(p['id'], lambda p: p.update(clips=[c]))
    edit(editor, 'frame_shot', shot_id='s0', frame=dict(zoom=1.5, center=.5))
    edit(editor, 'frame_shot', shot_id='s1', frame=dict(zoom=1.5, center=0))
    result = edit(editor, 'shot_boundary', shot_id='s1', edge='start', at=1.04)['clips'][0]
    assert result['source_shots'][0]['end'] == result['source_shots'][1]['start'] == 1.041
    assert result['frame_suggestions'][0]['start'] == 1.041
    assert [(f['start'], f['end'], f['center']) for f in scenes_for(result)] == [
        (0, 1.041, .5), (1.041, 2, 0), (2, 3, .5)]
    assert result['source_shots_manual']


def test_manual_split_merge_do_not_change_video_or_render(editor):
    service, p = editor
    c = p['clips'][0]
    before = render_key(p, c)
    result = edit(editor, 'shot_split', at=.51)['clips'][0]
    assert [(s['start'], s['end']) for s in result['source_shots']] == [(0, .5), (.5, 1), (1, 2), (2, 3)]
    assert result['cuts'] == [] and result['hook'] == c['hook']
    assert render_key(p, result) == before
    result = edit(editor, 'shot_merge', shot_id='s0')['clips'][0]
    assert [(s['start'], s['end']) for s in result['source_shots']] == [(0, 1), (1, 2), (2, 3)]
    assert render_key(p, result) == before


@pytest.mark.parametrize('action,fields', [
    ('shot_split', dict(at=float('nan'))),
    ('shot_split', dict(at=True)),
    ('shot_split', dict(at=1)),
    ('shot_split', dict(at=-1)),
    ('shot_boundary', dict(shot_id='s0', edge='start', at=.2)),
    ('shot_boundary', dict(shot_id='s1', edge='start', at=2)),
    ('frame_shot', dict(shot_id='missing', frame=dict(zoom=1, center=0))),
    ('frame_shot', dict(shot_id='s1', frame=dict(zoom=9, center=0))),
    ('frame_shot', dict(shot_id='s1', frame=dict(zoom=1))),
])
def test_invalid_edits_are_atomic(editor, action, fields):
    service, p = editor
    before = service.store.load(p['id'])
    with pytest.raises(ValueError):
        edit(editor, action, **fields)
    assert service.store.load(p['id']) == before


def test_stale_or_busy_shot_edits_are_rejected(editor, monkeypatch):
    service, p = editor
    with pytest.raises(Conflict):
        edit(editor, 'shot_split', at=.5, revision=0)
    monkeypatch.setattr(service.jobs, 'busy', lambda pid: True)
    with pytest.raises(ValueError):
        edit(editor, 'shot_split', at=.5)


def test_changed_short_bounds_invalidate_shot_coverage(editor):
    result = edit(editor, 'update', changes=dict(end=4))['clips'][0]
    assert 'source_shots' not in result


def test_ai_refresh_saves_shots_but_does_not_apply_them(editor, monkeypatch):
    from shortsmaker import framing
    from types import SimpleNamespace
    service, p = editor
    monkeypatch.setattr(service, 'source', lambda p: None)
    def analyze(ctx, project, clip, *args):
        clip['source_shots'][0]['boundary_kind'] = 'detected'
        return [dict(id='new', start=0, end=1, zoom=1.5, center=0)]
    monkeypatch.setattr(framing, 'analyze', analyze)
    service.framing(SimpleNamespace(progress=lambda *args: None, check=lambda: None), p['id'], {})
    saved = service.store.load(p['id'])['clips'][0]
    assert saved['source_shots'][0]['boundary_kind'] == 'detected'
    assert saved['frame_overrides'] == []
    assert saved['frame_analysis_version'] == 3
