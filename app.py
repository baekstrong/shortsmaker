"""Loopback-only local app. Start with ./시작.command."""

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from flask import Flask, request, jsonify, send_file
from werkzeug.exceptions import HTTPException
from shortsmaker import ai, media
from shortsmaker.store import Store, Conflict
from shortsmaker.jobs import Jobs
from shortsmaker.service import Service
from shortsmaker.publishing import Connections, Publisher
from shortsmaker.workflow import Workflow

ROOT = Path(__file__).resolve().parent


def create_app(data_dir=None):
    app = Flask(__name__, static_folder="static")
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    store = Store(data_dir or os.environ.get("SHORTSMAKER_DATA", ROOT / "data"))
    jobs = Jobs(store.root)
    service = Service(store, jobs)
    publisher = Publisher(store, jobs, Connections(ROOT / ".env.local"))
    workflow = Workflow(store, jobs, service, publisher)
    app.extensions.update(store=store, jobs=jobs, service=service, publisher=publisher, workflow=workflow)

    @app.before_request
    def local_only():
        if request.host.split(":")[0] not in ("127.0.0.1", "localhost", "[::1]"):
            return jsonify(error="로컬 주소로 접속해 주세요."), 403
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host:
            return jsonify(error="다른 사이트에서 요청할 수 없습니다."), 403
        if (
            request.method not in ("GET", "HEAD", "OPTIONS")
            and request.headers.get("X-Shortsmaker") != "1"
        ):
            return jsonify(error="앱 화면에서 실행해 주세요."), 403

    @app.errorhandler(Exception)
    def error(exc):
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description), exc.code
        code = (
            409
            if isinstance(exc, Conflict)
            else (
                400
                if isinstance(exc, (ValueError, KeyError, FileNotFoundError))
                else 500
            )
        )
        return jsonify(error=str(exc)), code

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.post("/api/projects/<pid>/reconcile")
    def reconcile(pid):
        if any(j["status"] in ("running", "queued") for j in jobs.list()):
            raise ValueError("진행 중인 작업이 끝난 뒤 연결해 주세요.")
        publisher.reconcile(
            pid,
            request.json["record_id"],
            request.json["channel_id"],
            request.json["post_id"],
        )
        return jsonify(ok=True)

    @app.post("/api/projects/<pid>/calendar")
    def calendar(pid):
        return jsonify(workflow.make_plan(pid, request.json or {}))

    @app.post("/api/projects/<pid>/calendar/<plan_id>/confirm")
    def confirm_calendar(pid, plan_id):
        return jsonify(workflow.confirm(pid, plan_id))

    @app.get("/api/channels")
    def channels():
        return jsonify(channels=publisher.connections.channels())

    @app.get("/api/reservations")
    def reservations():
        return jsonify(records=publisher.records())

    def present(project):
        for clip in project["clips"]:
            render = clip.get("render")
            clip["render_current"] = bool(
                render
                and render["key"] == media.render_key(project, clip)
                and Path(render["path"]).is_file()
            )
        return project

    @app.get("/api/state")
    def state():
        return jsonify(
            projects=[present(p) for p in store.list()],
            jobs=jobs.list(),
            models=ai.models(),
        )

    @app.post("/api/choose-file")
    def choose_file():
        result = subprocess.run(
            [
                "osascript",
                "-e",
                'POSIX path of (choose file with prompt "편집한 롱폼 영상을 선택하세요")',
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode:
            return jsonify(cancelled=True)
        return jsonify(path=result.stdout.strip())

    @app.post("/api/projects")
    def create_project():
        path = media.existing_path(request.json["path"])
        metadata = media.probe(path)
        if metadata["width"] < metadata["height"]:
            raise ValueError("가로 롱폼 원본을 선택해 주세요.")
        return jsonify(store.create(path, metadata))

    @app.post("/api/projects/<pid>/edit")
    def edit(pid):
        return jsonify(present(service.edit(pid, request.json)))

    @app.post("/api/projects/<pid>/reconnect")
    def reconnect(pid):
        return jsonify(present(service.reconnect(pid, request.json["path"])))

    @app.post("/api/projects/<pid>/jobs/<kind>")
    def submit(pid, kind):
        store.load(pid)
        if kind == "encode":
            from shortsmaker.service import export_folder
            export_folder(store.load(pid))
        return jsonify(jobs.submit(pid, kind, request.json or {}))

    @app.post("/api/jobs/<jid>/<action>")
    def job_action(jid, action):
        if action == "cancel":
            jobs.cancel(jid)
            return jsonify(ok=True)
        if action == "retry":
            return jsonify(jobs.retry(jid))
        raise ValueError("잘못된 작업입니다.")

    @app.get("/api/projects/<pid>/source")
    def source(pid):
        p = store.load(pid)
        return send_file(service.source(p), conditional=True)

    @app.get("/api/projects/<pid>/clips/<cid>/title.png")
    def title(pid, cid):
        p = store.load(pid)
        c = next(c for c in p["clips"] if c["id"] == cid)
        path = store.folder(pid) / "titles" / (media.render_key(p, c) + ".png")
        valid = False
        if path.exists():
            try:
                with media.Image.open(path) as cached:
                    cached.verify()
                valid = True
            except (OSError, SyntaxError):
                pass
        if not valid:
            media.title_image(
                c["hook"] or "후킹 문구를 선택하세요",
                c.get("yellow", ""),
                path,
                c.get("font_size", 80),
            )
        return send_file(path, conditional=True)

    @app.get("/api/projects/<pid>/clips/<cid>/information/<sid>.jpg")
    def information_thumbnail(pid, cid, sid):
        p = store.load(pid)
        c = next(c for c in p["clips"] if c["id"] == cid)
        s = next(s for s in (c.get("frame_suggestions") or []) if s.get("id") == sid)
        name = s.get("thumbnail", "")
        if not name or Path(name).name != name:
            raise ValueError("설명 화면 미리보기가 없습니다.")
        path = store.folder(pid) / "frames" / cid / name
        if not path.is_file():
            media.information_image(service.source(p), s["time"], path)
        return send_file(path, conditional=True)

    @app.get("/api/projects/<pid>/clips/<cid>/output")
    def output(pid, cid):
        p = store.load(pid)
        c = next(c for c in p["clips"] if c["id"] == cid)
        r = c.get("render")
        if not r or r["key"] != media.render_key(p, c):
            raise ValueError("현재 편집 내용으로 인코딩해 주세요.")
        return send_file(
            r["path"],
            conditional=True,
            as_attachment=request.args.get("download") == "1",
        )

    @app.post("/api/projects/<pid>/export-folder")
    def choose_export_folder(pid):
        from shortsmaker.service import export_folder
        store.load(pid)
        result = subprocess.run(["osascript", "-e", 'POSIX path of (choose folder with prompt "완성 영상을 저장할 폴더를 선택하세요" )'], capture_output=True, text=True, timeout=180)
        if result.returncode:
            if "-128" in result.stderr:
                return jsonify(cancelled=True)
            raise ValueError("저장 폴더를 선택하지 못했습니다. 다시 시도해 주세요.")
        folder = export_folder({"export_dir": result.stdout.strip()})
        with jobs.lock:
            if jobs.busy(pid):
                raise ValueError("진행 중인 작업이 끝난 뒤 저장 위치를 변경해 주세요.")
            p = store.change(pid, lambda p: p.update(export_dir=str(folder)))
        return jsonify(present(p))

    @app.post("/api/projects/<pid>/reveal")
    def reveal(pid):
        from shortsmaker.service import export_folder
        folder = export_folder(store.load(pid))
        subprocess.run(["open", str(folder)], check=True)
        return jsonify(ok=True)

    return app


if __name__ == "__main__":
    import fcntl

    data_root = Path(os.environ.get("SHORTSMAKER_DATA", ROOT / "data"))
    data_root.mkdir(parents=True, exist_ok=True)
    lockfile = (data_root / "server.lock").open("a")
    try:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("이미 이 데이터 폴더를 사용하는 앱이 실행 중입니다.")
    app = create_app(data_root)
    import signal

    def shutdown(signum, frame):
        app.extensions["jobs"].shutdown()
        raise SystemExit(0)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, shutdown)

    def maintenance():
        while True:
            try:
                jobs = app.extensions["jobs"]
                records = app.extensions["publisher"].records()
                if any(not r.get("deleted_at") for r in records) and not any(
                    j["status"] in ("running", "queued") for j in jobs.list()
                ):
                    jobs.submit("__maintenance", "refresh")
            except Exception:
                app.logger.exception("예약 상태 자동 확인을 시작하지 못했습니다.")
            time.sleep(3600)

    threading.Thread(target=maintenance, daemon=True).start()
    app.run(
        host="127.0.0.1",
        port=int(os.environ.get("SHORTSMAKER_PORT", "5099")),
        threaded=True,
    )
