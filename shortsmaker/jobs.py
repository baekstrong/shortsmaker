"""Single heavy-job queue; durable states, cooperative and subprocess cancellation."""
import json
import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path
from .store import atomic_json, now, uid


class Cancelled(Exception):
    pass


class Context:
    def __init__(self, manager, job):
        self.manager, self.job = manager, job
        self.cancelled = threading.Event()

    def check(self):
        if self.cancelled.is_set():
            raise Cancelled('작업을 중단했습니다. 완료된 단계는 보존됩니다.')

    def progress(self, message, percent=None):
        self.check()
        self.manager.update(self.job['id'], message=message, progress=percent)

    def run(self, args, *, cwd=None, input_text=None, timeout=3600, on_line=None):
        self.check()
        work = self.manager.root / self.job['id']
        work.mkdir(parents=True, exist_ok=True)
        output = work / ('process-' + uid() + '.log')
        with output.open('w+') as log:
            proc = subprocess.Popen([str(a) for a in args], cwd=cwd,
                                    stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                if input_text is not None:
                    proc.stdin.write(input_text.encode())
                    proc.stdin.close()
                started, pos = time.monotonic(), 0
                while proc.poll() is None:
                    self.check()
                    if time.monotonic() - started > timeout:
                        raise TimeoutError('작업 제한 시간을 넘었습니다. 완료된 단계부터 다시 시도할 수 있습니다.')
                    if on_line:
                        with output.open() as reader:
                            reader.seek(pos)
                            for line in reader:
                                on_line(line.rstrip())
                            pos = reader.tell()
                    time.sleep(.2)
                self.check()
                content = output.read_text(errors='replace')
                if proc.returncode:
                    raise RuntimeError(f'{Path(args[0]).name} 실행 실패: {content[-1800:]}')
                return content
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()


class Jobs:
    def __init__(self, root):
        self.root = Path(root) / 'jobs'
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.queue = queue.Queue()
        self.contexts, self.handlers = {}, {}
        self.items = {}
        for file in self.root.glob('*.json'):
            job = json.loads(file.read_text())
            if job['status'] in ('queued', 'running'):
                job.update(status='interrupted', message='앱이 종료되어 중단되었습니다. 재시도하면 저장된 단계부터 진행합니다.', finished_at=now())
                atomic_json(file, job)
            self.items[job['id']] = job
        threading.Thread(target=self._worker, daemon=True).start()

    def update(self, job_id, **values):
        with self.lock:
            self.items[job_id].update(values)
            atomic_json(self.root / (job_id + '.json'), self.items[job_id])

    def list(self):
        with self.lock:
            return sorted([dict(j) for j in self.items.values()], key=lambda j: j['created_at'], reverse=True)

    def busy(self, project_id):
        return any(j['project_id'] == project_id and j['status'] in ('queued', 'running') for j in self.list())

    def submit(self, project_id, kind, args=None):
        with self.lock:
            if self.busy(project_id):
                raise ValueError('이 프로젝트에 진행 중인 작업이 있습니다. 완료하거나 중단한 뒤 진행해 주세요.')
            if kind not in self.handlers:
                raise ValueError('지원하지 않는 작업입니다.')
            job = dict(id=uid(), project_id=project_id, kind=kind, args=args or {},
                       status='queued', message='순서를 기다리고 있습니다.', progress=0, created_at=now())
            self.items[job['id']] = job
            self.update(job['id'])
            self.queue.put(job['id'])
            return dict(job)

    def cancel(self, job_id):
        with self.lock:
            job = self.items[job_id]
            if job['status'] == 'queued':
                self.update(job_id, status='cancelled', finished_at=now(), message='대기 중인 작업을 취소했습니다.')
            elif job_id in self.contexts:
                self.contexts[job_id].cancelled.set()

    def retry(self, job_id):
        job = self.items[job_id]
        if job['status'] not in ('failed', 'cancelled', 'interrupted'):
            raise ValueError('중단되거나 실패한 작업만 재시도할 수 있습니다.')
        return self.submit(job['project_id'], job['kind'], job['args'])

    def _worker(self):
        while True:
            job_id = self.queue.get()
            job = self.items[job_id]
            try:
                with self.lock:
                    if job['status'] != 'queued':
                        continue
                    ctx = Context(self, job)
                    self.contexts[job_id] = ctx
                    self.update(job_id, status='running', started_at=now())
                self.handlers[job['kind']](ctx, job['project_id'], job['args'])
                ctx.check()
                self.update(job_id, status='succeeded', message='완료했습니다.', progress=100, finished_at=now())
            except Cancelled as exc:
                self.update(job_id, status='cancelled', message=str(exc), finished_at=now())
            except Exception as exc:
                self.update(job_id, status='failed', message=str(exc)[-2000:], finished_at=now())
            finally:
                self.contexts.pop(job_id, None)
                self.queue.task_done()
