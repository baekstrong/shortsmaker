"""Atomic local persistence. Mutations share a lock; revisions prevent stale UI writes."""

import copy
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uid() + ".tmp")
    try:
        with tmp.open("w") as f:
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def remove_automatic_frames(self):
        """Retire AI-applied positions, including undo snapshots; keep a local backup."""
        with self.lock:
            for path in (self.root / "projects").glob("*/project.json"):
                project = json.loads(path.read_text())
                snapshots = [project["clips"], *project.get("history", [])]
                if not any(f.get("origin") == "ai" for clips in snapshots
                           for clip in clips for f in clip.get("frame_overrides", [])):
                    continue
                backup = self.root / "backups" / "suggestions-only" / path.parent.name / "project.json"
                if not backup.exists():
                    atomic_json(backup, project)
                for clips in snapshots:
                    for clip in clips:
                        if "frame_overrides" in clip:
                            clip["frame_overrides"] = [f for f in clip["frame_overrides"]
                                                       if f.get("origin") != "ai"]
                project["revision"] += 1
                atomic_json(path, project)

    def folder(self, project_id):
        if not re.fullmatch(r"[a-f0-9]{32}", project_id):
            raise ValueError("잘못된 프로젝트 ID입니다.")
        return self.root / "projects" / project_id

    def load(self, project_id):
        with self.lock:
            return json.loads((self.folder(project_id) / "project.json").read_text())

    def list(self):
        with self.lock:
            result = [
                json.loads(p.read_text())
                for p in (self.root / "projects").glob("*/project.json")
            ]
            return sorted(
                (p for p in result if not p.get("deleted_at")),
                key=lambda p: p["updated_at"], reverse=True,
            )

    def delete(self, project_id):
        """Remove from the project list while preserving media and reservation references."""
        return self.change(project_id, lambda p: p.update(deleted_at=now()))

    def create(self, source, metadata):
        p = dict(
            id=uid(),
            name=Path(source).stem,
            source=str(source),
            metadata=metadata,
            revision=0,
            created_at=now(),
            updated_at=now(),
            clips=[],
            transcript=[],
            model="gpt-6-astra",
            effort="medium",
            stt_model="medium",
            history=[],
        )
        atomic_json(self.folder(p["id"]) / "project.json", p)
        return p

    def change(self, project_id, mutate, revision=None, history=False):
        with self.lock:
            p = self.load(project_id)
            if revision is not None and p["revision"] != revision:
                raise Conflict(
                    "다른 작업이 먼저 저장되었습니다. 새로고침한 뒤 다시 수정해 주세요."
                )
            if history:
                p["history"] = (p.get("history", []) + [copy.deepcopy(p["clips"])])[
                    -30:
                ]
            mutate(p)
            p["revision"] += 1
            p["updated_at"] = now()
            atomic_json(self.folder(project_id) / "project.json", p)
            return p
