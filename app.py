"""Loopback-only local app. Start with ./시작.command."""
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from flask import Flask,request,jsonify,send_file
from werkzeug.exceptions import HTTPException
from shortsmaker import ai,media
from shortsmaker.store import Store,Conflict
from shortsmaker.jobs import Jobs
from shortsmaker.service import Service
from shortsmaker.publishing import Connections,Publisher

ROOT=Path(__file__).resolve().parent


def create_app(data_dir=None):
    app=Flask(__name__,static_folder='static')
    app.config['MAX_CONTENT_LENGTH']=1024*1024
    store=Store(data_dir or os.environ.get('SHORTSMAKER_DATA',ROOT/'data'))
    jobs=Jobs(store.root)
    service=Service(store,jobs)
    publisher=Publisher(store,jobs,Connections(ROOT/'.env.local'))
    app.extensions.update(store=store,jobs=jobs,service=service,publisher=publisher)

    @app.before_request
    def local_only():
        if request.host.split(':')[0] not in ('127.0.0.1','localhost','[::1]'):
            return jsonify(error='로컬 주소로 접속해 주세요.'),403
        origin=request.headers.get('Origin')
        if origin and urlparse(origin).netloc!=request.host:
            return jsonify(error='다른 사이트에서 요청할 수 없습니다.'),403
        if request.method not in ('GET','HEAD','OPTIONS') and request.headers.get('X-Shortsmaker')!='1':
            return jsonify(error='앱 화면에서 실행해 주세요.'),403

    @app.errorhandler(Exception)
    def error(exc):
        if isinstance(exc,HTTPException): return jsonify(error=exc.description),exc.code
        code=409 if isinstance(exc,Conflict) else 400 if isinstance(exc,(ValueError,KeyError,FileNotFoundError)) else 500
        return jsonify(error=str(exc)),code

    @app.get('/')
    def index(): return app.send_static_file('index.html')

    @app.post('/api/projects/<pid>/reconcile')
    def reconcile(pid):
        if any(j['status'] in ('running','queued') for j in jobs.list()):
            raise ValueError('진행 중인 작업이 끝난 뒤 연결해 주세요.')
        publisher.reconcile(pid,request.json['record_id'],request.json['channel_id'],request.json['post_id'])
        return jsonify(ok=True)

    @app.get('/api/channels')
    def channels(): return jsonify(channels=publisher.connections.channels())

    @app.get('/api/reservations')
    def reservations(): return jsonify(records=publisher.records())

    @app.get('/api/state')
    def state():
        return jsonify(projects=store.list(),jobs=jobs.list(),models=ai.models())

    @app.post('/api/choose-file')
    def choose_file():
        result=subprocess.run(['osascript','-e','POSIX path of (choose file with prompt "편집한 롱폼 영상을 선택하세요")'],capture_output=True,text=True,timeout=180)
        if result.returncode: return jsonify(cancelled=True)
        return jsonify(path=result.stdout.strip())

    @app.post('/api/projects')
    def create_project():
        path=media.existing_path(request.json['path'])
        metadata=media.probe(path)
        if metadata['width']<metadata['height']:
            raise ValueError('가로 롱폼 원본을 선택해 주세요.')
        return jsonify(store.create(path,metadata))

    @app.post('/api/projects/<pid>/edit')
    def edit(pid): return jsonify(service.edit(pid,request.json))

    @app.post('/api/projects/<pid>/jobs/<kind>')
    def submit(pid,kind):
        store.load(pid)
        return jsonify(jobs.submit(pid,kind,request.json or {}))

    @app.post('/api/jobs/<jid>/<action>')
    def job_action(jid,action):
        if action=='cancel': jobs.cancel(jid); return jsonify(ok=True)
        if action=='retry': return jsonify(jobs.retry(jid))
        raise ValueError('잘못된 작업입니다.')

    @app.get('/api/projects/<pid>/source')
    def source(pid):
        p=store.load(pid)
        return send_file(service.source(p),conditional=True)

    @app.get('/api/projects/<pid>/clips/<cid>/title.png')
    def title(pid,cid):
        p=store.load(pid)
        c=next(c for c in p['clips'] if c['id']==cid)
        path=store.folder(pid)/'titles'/(media.render_key(p,c)+'.png')
        if not path.exists(): media.title_image(c['hook'] or '후킹 문구를 선택하세요',c.get('yellow',''),path,c.get('font_size',80))
        return send_file(path,conditional=True)

    @app.get('/api/projects/<pid>/clips/<cid>/output')
    def output(pid,cid):
        p=store.load(pid)
        c=next(c for c in p['clips'] if c['id']==cid)
        r=c.get('render')
        if not r or r['key']!=media.render_key(p,c): raise ValueError('현재 편집 내용으로 인코딩해 주세요.')
        return send_file(r['path'],conditional=True,as_attachment=request.args.get('download')=='1')

    @app.post('/api/projects/<pid>/reveal')
    def reveal(pid):
        folder=store.folder(pid)
        subprocess.run(['open',str(folder)],check=True)
        return jsonify(ok=True)

    return app


if __name__=='__main__':
    import fcntl
    data_root=Path(os.environ.get('SHORTSMAKER_DATA',ROOT/'data'))
    data_root.mkdir(parents=True,exist_ok=True)
    lockfile=(data_root/'server.lock').open('a')
    try:
        fcntl.flock(lockfile,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('이미 이 데이터 폴더를 사용하는 앱이 실행 중입니다.')
    app=create_app(data_root)
    def maintenance():
        while True:
            try:
                jobs=app.extensions['jobs']
                records=app.extensions['publisher'].records()
                if any(not r.get('deleted_at') for r in records) and not any(j['status'] in ('running','queued') for j in jobs.list()):
                    jobs.submit('__maintenance','refresh')
            except Exception:
                app.logger.exception('예약 상태 자동 확인을 시작하지 못했습니다.')
            time.sleep(3600)
    threading.Thread(target=maintenance,daemon=True).start()
    app.run(host='127.0.0.1',port=int(os.environ.get('SHORTSMAKER_PORT','5099')),threaded=True)
