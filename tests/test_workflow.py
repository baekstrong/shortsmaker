import copy
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from shortsmaker.workflow import Workflow, calendar_days, noon, KST
from shortsmaker.store import Store
from shortsmaker.service import new_clip
from shortsmaker.media import render_key
from shortsmaker.publishing import Publisher, BufferError


class Jobs:
    def __init__(self):
        self.lock = threading.RLock()
        self.handlers = {}
        self.items = {}
    def list(self): return list(self.items.values())
    def busy(self, pid): return any(j['project_id'] == pid and j['status'] in ('running','queued') for j in self.list())
    def update(self, id, **values): self.items[id].update(values)
    def submit(self, pid, kind, args):
        id = str(len(self.items))
        j = dict(id=id, project_id=pid, kind=kind, args=copy.deepcopy(args), status='queued')
        self.items[id] = j
        return j


class Context:
    def __init__(self, jobs, job): self.manager, self.job = jobs, job
    def progress(self, *args): pass
    def check(self): pass


class Connections:
    def __init__(self): self.remote = []; self.created = []; self.uploads = []; self.fail = None
    def channels(self):
        return [dict(id=s, name=s, displayName=s, service=s, organization_id='org', isQueuePaused=False) for s in ('instagram','youtube')]
    def scheduled_posts(self, channels): return copy.deepcopy(self.remote)
    def upload(self, path, key, ctx): self.uploads.append(path); return 'https://example.test/'+key
    def create(self, channel, text, due, url, privacy):
        if self.fail == channel['id']: raise BufferError('unknown network outcome')
        p = dict(id=str(len(self.remote)), channelId=channel['id'], text=text, dueAt=due, sentAt=None, status='scheduled')
        self.remote.append(p); self.created.append(p)
        return p
    def post(self, id): return next(p for p in self.remote if p['id'] == id)


class Service:
    def __init__(self, store): self.store=store; self.calls=[]; self.fail=None
    def encode(self, ctx, pid, args):
        self.calls.append('encode')
        if self.fail == 'encode': raise RuntimeError('encode failed')
        def save(p):
            for c in p['clips']:
                path = self.store.folder(pid)/(c['id']+'.mp4'); path.write_bytes(b'test')
                c['render'] = dict(key=render_key(p,c),path=str(path))
        self.store.change(pid,save)
    def stage(self, name, ctx, pid, args):
        self.calls.append(name)
        if self.fail == name: raise RuntimeError(name+' failed')
        self.store.change(pid, lambda p: p.update(summary=name))
    def analyze(self,*args): self.stage('analyze',*args)
    def hooks(self,*args): self.stage('hooks',*args)
    def framing(self,*args): self.stage('framing',*args)


def setup(tmp_path):
    store=Store(tmp_path); jobs=Jobs(); conn=Connections(); publisher=Publisher(store,jobs,conn); service=Service(store)
    w=Workflow(store,jobs,service,publisher)
    p=store.create('/tmp/source.mp4',dict(width=1920,height=1080,duration=100,size=1,mtime_ns=1))
    clips=[new_clip(i*10,(i+1)*10) for i in range(3)]
    for i,c in enumerate(clips): c.update(hook=f'쇼츠 {i+1}',yellow='')
    p=store.change(p['id'],lambda p:p.update(clips=clips))
    return w,p,conn,service


def test_noon_calendar_skips_occupied_and_crosses_year():
    clips=[dict(id=str(i),hook=str(i)) for i in range(4)]
    items=calendar_days(clips,'2026-12-30',{'2026-12-31','2027-01-02'},datetime(2026,12,1,tzinfo=KST))
    assert [i['date'] for i in items] == ['2026-12-30','2027-01-01','2027-01-03','2027-01-04']
    assert all(i['due_at'].endswith('T12:00:00+09:00') for i in items)
    with pytest.raises(ValueError): calendar_days(clips,'2026-12-01',set(),datetime(2026,12,1,12,tzinfo=KST))


def test_prepare_order_and_retry_resumes_without_resplitting(tmp_path):
    w,p,conn,service=setup(tmp_path)
    j=w.jobs.submit(p['id'],'prepare',{});ctx=Context(w.jobs,j)
    service.fail='hooks'
    with pytest.raises(RuntimeError): w.prepare(ctx,p['id'],j['args'])
    assert j['args']['completed'] == ['analyze']
    service.fail=None
    w.prepare(ctx,p['id'],j['args'])
    assert service.calls == ['analyze','hooks','hooks','framing']
    assert [e['stage'] for e in j['stage_events']] == ['analyze','hooks','framing']
    assert not conn.created and not any(c['confirmed'] for c in w.store.load(p['id'])['clips'])


def test_prepare_retry_rejects_intervening_edit(tmp_path):
    w,p,conn,service=setup(tmp_path)
    j=w.jobs.submit(p['id'],'prepare',{});ctx=Context(w.jobs,j)
    service.fail='hooks'
    with pytest.raises(RuntimeError): w.prepare(ctx,p['id'],j['args'])
    w.store.change(p['id'],lambda p:p.update(summary='edited'))
    with pytest.raises(ValueError,match='편집'): w.prepare(ctx,p['id'],j['args'])


def test_calendar_is_read_only_until_idempotent_confirmation(tmp_path):
    w,p,conn,service=setup(tmp_path)
    plan=w.make_plan(p['id'],{})
    assert not service.calls and not conn.created and not conn.uploads
    assert not any(c['confirmed'] for c in w.store.load(p['id'])['clips'])
    j=w.confirm(p['id'],plan['id']);same=w.confirm(p['id'],plan['id'])
    assert j['id']==same['id'] and len(w.jobs.items)==1
    assert not conn.created and all(c['confirmed'] for c in w.store.load(p['id'])['clips'])
    j['status']='running';w.publish(Context(w.jobs,j),p['id'],j['args']);j['status']='succeeded'
    assert service.calls==['encode'] and len(conn.created)==6
    assert {x['dueAt'] for x in conn.created}=={noon(date.fromisoformat(i['date'])).astimezone(timezone.utc).isoformat() for i in plan['items']}
    assert [e['stage'] for e in j['stage_events']]==['encode','schedule']
    assert w.load(plan['id'])['status']=='completed'


def test_calendar_checks_external_conflicts_and_edited_content(tmp_path):
    w,p,conn,service=setup(tmp_path);plan=w.make_plan(p['id'],{})
    conn.remote.append(dict(id='existing',channelId='instagram',text='외부 예약',dueAt=plan['items'][0]['due_at'],status='scheduled'))
    with pytest.raises(ValueError,match='다른 예약'): w.confirm(p['id'],plan['id'])
    plan=w.make_plan(p['id'],{})
    assert plan['items'][0]['date'] != conn.remote[0]['dueAt'][:10]
    w.store.change(p['id'],lambda p:p['clips'][0].update(hook='수정'))
    with pytest.raises(ValueError,match='편집'): w.confirm(p['id'],plan['id'])
    assert not conn.created


def test_encode_failure_prevents_all_uploads_and_retry_reuses_calendar(tmp_path):
    w,p,conn,service=setup(tmp_path);plan=w.make_plan(p['id'],{});j=w.confirm(p['id'],plan['id']);ctx=Context(w.jobs,j)
    service.fail='encode'
    with pytest.raises(RuntimeError):w.publish(ctx,p['id'],j['args'])
    assert not conn.uploads and not conn.created
    service.fail=None;w.publish(ctx,p['id'],j['args'])
    assert len(conn.created)==6
    w.publish(ctx,p['id'],j['args'])
    assert len(conn.created)==6  # Existing posts ignored during collision checks, never duplicated.


def test_unknown_reservation_retry_never_recreates_successful_channel(tmp_path):
    w,p,conn,service=setup(tmp_path);plan=w.make_plan(p['id'],{});j=w.confirm(p['id'],plan['id']);ctx=Context(w.jobs,j)
    conn.fail='youtube'
    with pytest.raises(BufferError): w.publish(ctx,p['id'],j['args'])
    assert len(conn.created)==1
    conn.fail=None
    with pytest.raises(ValueError,match='불명확'): w.publish(ctx,p['id'],j['args'])
    assert len(conn.created)==1


def test_other_project_approved_plan_holds_dates(tmp_path):
    w,p,conn,service=setup(tmp_path);plan=w.make_plan(p['id'],{});w.confirm(p['id'],plan['id'])
    other=w.store.create('/tmp/other.mp4',p['metadata'])
    w.store.change(other['id'],lambda p:p.update(clips=[dict(new_clip(0,10),hook='다른 영상')]))
    later=w.make_plan(other['id'],{})
    assert later['items'][0]['date']>plan['items'][-1]['date']


def test_unapproved_plan_cannot_be_executed(tmp_path):
    w,p,conn,service=setup(tmp_path);plan=w.make_plan(p['id'],{})
    j=w.jobs.submit(p['id'],'publish_plan',{'plan_id':plan['id']})
    with pytest.raises(ValueError,match='확정'): w.publish(Context(w.jobs,j),p['id'],j['args'])
    assert not conn.created and not service.calls


def test_expired_approved_dates_never_shift_automatically(tmp_path):
    w,p,conn,service=setup(tmp_path)
    plan=w.make_plan(p['id'],{})
    j=w.confirm(p['id'],plan['id'])
    expired=w.load(plan['id'])
    expired['items'][0]['due_at']='2000-01-01T12:00:00+09:00'
    w.save(expired)
    with pytest.raises(ValueError,match='자동 변경하지'):
        w.publish(Context(w.jobs,j),p['id'],j['args'])
    assert not service.calls and not conn.created
    assert w.load(plan['id'])['items'][0]['due_at']=='2000-01-01T12:00:00+09:00'
