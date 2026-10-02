import copy
import pytest
from shortsmaker import ai, media
from shortsmaker.timeline import kept_ranges, clip_duration, validate_cuts, transcript_for
from shortsmaker.service import Service, new_clip
from shortsmaker.store import Store
from shortsmaker.jobs import Jobs


def test_removed_content_changes_render_and_preserves_source_framing():
    c = new_clip(10, 30)
    p = dict(source='/tmp/source.mp4', metadata={})
    old = media.render_key(p, c)
    c.update(cuts=[dict(start=10, end=12, reason='도입'), dict(start=16, end=20, reason='화면 조정')],
             frame_overrides=[dict(id='position', start=15, end=25, zoom=1, center=.3)])
    validate_cuts(c)
    assert clip_duration(c) == 14
    assert kept_ranges(c) == [dict(start=12, end=16), dict(start=20, end=30)]
    assert [(s['start'],s['end'],s['zoom']) for s in media.scenes_for(c)] == [
        (12,15,1.5),(15,16,1),(20,25,1),(25,30,1.5)]
    assert old != media.render_key(p, c)


@pytest.mark.parametrize('cuts', [
    [dict(start=9,end=11,reason='범위 밖')],
    [dict(start=15,end=18,reason='삭제'),dict(start=17,end=19,reason='겹침')],
    [dict(start=10,end=30,reason='전체')],
    [dict(start=15,end=float('nan'),reason='NaN')],
    [dict(start=15,end=16,reason='')],
    [dict(start=True,end=16,reason='불리언')],
])
def test_invalid_cuts_are_rejected(cuts):
    with pytest.raises(ValueError): validate_cuts(dict(start=10,end=30,cuts=cuts))


def test_hook_prompt_uses_only_kept_words_and_actual_opening(tmp_path, monkeypatch):
    p = dict(model='gpt-6-astra', effort='medium', transcript=[
        dict(start=0,end=4,text='홍보 핵심 설명',words=[
            dict(start=0,end=2,text='홍보'),dict(start=2,end=3,text='핵심'),dict(start=3,end=4,text='설명')])])
    c = dict(start=0,end=4,cuts=[dict(start=0,end=2,reason='홍보')])
    assert transcript_for(p,c) == [dict(start=2,end=4,text='핵심 설명')]
    def call(ctx, prompt, *args, **kwargs):
        assert '홍보' not in prompt
        assert '첫8초 발언: 핵심 설명' in prompt
        return dict(hooks=[dict(text='왜 힘들까',yellow_phrase='힘들')],recommended_text='왜 힘들까')
    monkeypatch.setattr(ai,'call',call)
    ai.hooks(None,p,c,tmp_path)


def test_manual_cut_restore_split_merge_undo(tmp_path):
    store = Store(tmp_path); jobs = Jobs(tmp_path); service = Service(store,jobs)
    p = store.create('/tmp/source.mp4',dict(duration=100))
    c = new_clip(10,30);c.update(hook='후킹',yellow='후킹',confirmed=True)
    store.change(p['id'],lambda p:p.update(clips=[c]))
    p = service.edit(p['id'],dict(action='cut',clip_id=c['id'],start=16,end=20))
    assert not p['clips'][0]['confirmed'] and p['clips'][0]['hook']=='후킹'
    with pytest.raises(ValueError): service.edit(p['id'],dict(action='cut',clip_id=c['id'],start=17,end=22))
    assert store.load(p['id']) == p
    shortened = copy.deepcopy(p['clips'])
    p = service.edit(p['id'],dict(action='restore_cut',clip_id=c['id'],index=0))
    assert p['clips'][0]['cuts']==[]
    p = service.edit(p['id'],dict(action='undo'))
    assert p['clips']==shortened
    p = service.edit(p['id'],dict(action='split',clip_id=c['id'],at=18))
    assert [(x['cuts'][0]['start'],x['cuts'][0]['end']) for x in p['clips']]==[(16,18),(18,20)]
    p = service.edit(p['id'],dict(action='merge',clip_id=c['id']))
    assert p['clips'][0]['cuts']==shortened[0]['cuts']
    p = service.edit(p['id'],dict(action='update',clip_id=c['id'],changes=dict(start=19)))
    assert p['clips'][0]['cuts'][0]['start']==19
    jobs.shutdown()


def test_content_stage_applies_all_results_once_and_undo_restores(tmp_path, monkeypatch):
    store=Store(tmp_path);jobs=Jobs(tmp_path);service=Service(store,jobs)
    p=store.create('/tmp/source.mp4',dict(duration=100))
    clips=[new_clip(0,20),new_clip(20,40)]
    for c in clips:c.update(hook='후킹 보존',manual_frame=dict(zoom=1.2,center=.4),confirmed=True)
    p=store.change(p['id'],lambda p:p.update(clips=clips))
    monkeypatch.setattr(service,'source',lambda p:None)
    from shortsmaker import editing
    monkeypatch.setattr(editing,'review_images',lambda *args: ['frame.jpg'])
    def edit(ctx,p,c,folder,**kwargs):
        assert kwargs['images']==['frame.jpg']
        return dict(cuts=[dict(start=c['start']+1,end=c['start']+3,reason='화면 조정')],summary='화면 조정 삭제')
    monkeypatch.setattr(ai,'edit_content',edit)
    class Context:
        def progress(self,*args):pass
        def check(self):pass
        def abort_parallel(self):pass
    service.content_edit(Context(),p['id'],{})
    edited=store.load(p['id'])
    assert len(edited['history'])==1
    assert all(clip_duration(c)==18 and c['hook']=='후킹 보존' and c['manual_frame']==clips[0]['manual_frame'] and not c['confirmed'] for c in edited['clips'])
    service.content_edit(Context(),p['id'],{})
    assert store.load(p['id'])==edited
    assert service.edit(p['id'],dict(action='undo'))['clips']==clips
    jobs.shutdown()
