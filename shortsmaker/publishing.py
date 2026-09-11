"""R2 media and Buffer reservations with write-ahead delivery journal.
Unknown mutation outcomes never automatically retry; remote media stays available.
"""

import json
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
import boto3
from botocore.config import Config
from .store import atomic_json, now, uid
from .media import render_key


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("예약 시간에 시간대가 필요합니다.")
    return result.astimezone(timezone.utc)


def cleanup_due(record, at=None):
    deliveries = record.get("deliveries", [])
    if not deliveries or any(
        d.get("status") != "sent" or not d.get("sent_at") for d in deliveries
    ):
        return False
    try:
        sent = max(timestamp(d["sent_at"]) for d in deliveries)
        return (at or datetime.now(timezone.utc)) >= sent + timedelta(days=14)
    except (ValueError, TypeError):
        return False


class BufferError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class Connections:
    def __init__(self, env_path):
        self.env_path = Path(env_path)

    def config(self):
        cfg = {}
        if self.env_path.exists():
            for line in self.env_path.read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip().strip('"').strip("'")
        return cfg

    def configured(self):
        c = self.config()
        return {
            "buffer": bool(c.get("BUFFER_API_KEY")),
            "r2": all(
                c.get(k)
                for k in (
                    "R2_ACCOUNT_ID",
                    "R2_ACCESS_KEY_ID",
                    "R2_SECRET_ACCESS_KEY",
                    "R2_BUCKET",
                    "R2_PUBLIC_BASE_URL",
                )
            ),
        }

    def gql(self, query, variables=None):
        key = self.config().get("BUFFER_API_KEY")
        if not key:
            raise ValueError("설정 파일에 Buffer API 키가 필요합니다.")
        req = urllib.request.Request(
            "https://api.buffer.com",
            data=json.dumps({"query": query, "variables": variables or {}}).encode(),
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
                "User-Agent": "shortsmaker/0.1",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                result = json.load(r)
        except Exception as exc:
            # Never reflect authorization headers or signed request details into UI/logs.
            raise BufferError(
                "Buffer 응답을 확인하지 못했습니다. 네트워크 연결을 확인해 주세요."
            ) from exc
        if result.get("errors"):
            error = result["errors"][0]
            raise BufferError(
                error.get("message", "Buffer 요청 오류"),
                error.get("extensions", {}).get("code"),
            )
        return result["data"]

    def channels(self):
        organizations = self.gql("{account{organizations{id name}}}")["account"][
            "organizations"
        ]
        result = []
        for org in organizations:
            data = self.gql(
                "query($id:OrganizationId!){channels(input:{organizationId:$id}){id name displayName service isQueuePaused}}",
                {"id": org["id"]},
            )
            result.extend(
                dict(c, organization_id=org["id"])
                for c in data["channels"]
                if c["service"] in ("instagram", "youtube", "tiktok")
            )
        return result

    def scheduled_posts(self, channels):
        """Read every page; do not hide conflicts by returning a truncated queue."""
        result = []
        for org in sorted({c["organization_id"] for c in channels}):
            ids = [c["id"] for c in channels if c["organization_id"] == org]
            after = None
            for _ in range(100):
                data = self.gql(
                    "query($input:PostsInput!,$after:String){posts(input:$input,first:100,after:$after){edges{node{id channelId text status dueAt}} pageInfo{hasNextPage endCursor}}}",
                    {"input": {"organizationId": org, "filter": {
                        "channelIds": ids, "status": ["scheduled", "sending", "needs_approval", "error"]}},
                     "after": after},
                )["posts"]
                result.extend(e["node"] for e in data["edges"] or [])
                if not data["pageInfo"]["hasNextPage"]:
                    break
                cursor = data["pageInfo"]["endCursor"]
                if not cursor or cursor == after:
                    raise BufferError("Buffer 예약 목록을 끝까지 확인하지 못했습니다.")
                after = cursor
            else:
                raise BufferError("Buffer 예약 목록이 너무 많습니다. 확인 범위를 줄여 주세요.")
        return result

    def post(self, post_id):
        return self.gql(
            "query($input:PostInput!){post(input:$input){id status dueAt sentAt text channelId externalLink}}",
            {"input": {"id": post_id}},
        )["post"]

    def create(self, channel, text, due_at, url, privacy="public"):
        service = channel["service"]
        if service == "instagram":
            metadata = {"instagram": {"type": "reel", "shouldShareToFeed": True}}
        elif service == "youtube":
            metadata = {"youtube": {
                "title": text.replace("\n", " ")[:100],
                "categoryId": "17",
                "privacy": privacy,
                "notifySubscribers": False,
                "madeForKids": False,
            }}
        elif service == "tiktok":
            # Source footage is real; AI-assisted copy/cropping is not synthetic video.
            metadata = {"tiktok": {"isAiGenerated": False}}
        else:
            raise ValueError("지원하지 않는 발행 채널입니다.")
        data = self.gql(
            "mutation($input:CreatePostInput!){createPost(input:$input){__typename ... on PostActionSuccess{post{id status dueAt sentAt}} ... on MutationError{message}}}",
            {
                "input": {
                    "channelId": channel["id"],
                    "text": text,
                    "schedulingType": "automatic",
                    "mode": "customScheduled",
                    "dueAt": due_at,
                    "assets": [{"video": {"url": url}}],
                    "metadata": metadata,
                    "aiAssisted": True,
                }
            },
        )["createPost"]
        if data["__typename"] != "PostActionSuccess":
            raise BufferError(
                data.get("message", "예약 생성이 거절되었습니다."), "REJECTED"
            )
        return data["post"]

    def delete(self, post_id):
        data = self.gql(
            "mutation($input:DeletePostInput!){deletePost(input:$input){__typename ... on VoidMutationError{message}}}",
            {"input": {"id": post_id}},
        )["deletePost"]
        if data["__typename"] != "DeletePostSuccess":
            raise BufferError(data.get("message", "예약 삭제를 확인하지 못했습니다."))

    def r2(self):
        c = self.config()
        if not self.configured()["r2"]:
            raise ValueError("R2 설정을 먼저 완료해 주세요.")
        return (
            boto3.client(
                "s3",
                endpoint_url=f"https://{c['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                aws_access_key_id=c["R2_ACCESS_KEY_ID"],
                aws_secret_access_key=c["R2_SECRET_ACCESS_KEY"],
                region_name="auto",
                config=Config(
                    connect_timeout=10,
                    read_timeout=60,
                    retries={"max_attempts": 2},
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                ),
            ),
            c,
        )

    def upload(self, path, key, ctx):
        client, c = self.r2()
        size = Path(path).stat().st_size
        total = 0
        lock = threading.Lock()

        def progress(n):
            nonlocal total
            with lock:
                total += n
                ctx.progress(
                    f"예약용 영상 업로드 · {total/1024/1024:.1f}/{size/1024/1024:.1f} MB",
                    min(99, total / size * 100),
                )

        try:
            client.upload_file(
                str(path),
                c["R2_BUCKET"],
                key,
                ExtraArgs={"ContentType": "video/mp4"},
                Callback=progress,
            )
            url = c["R2_PUBLIC_BASE_URL"].rstrip("/") + "/" + key
            req = urllib.request.Request(
                url,
                method="HEAD",
                headers={"User-Agent": "shortsmaker-connectivity-check/1.0"},
            )
            with urllib.request.urlopen(req, timeout=30) as response:
                if (
                    response.status != 200
                    or int(response.headers.get("Content-Length", 0)) != size
                ):
                    raise ValueError("영상 공개 URL의 크기가 일치하지 않습니다.")
            return url
        except Exception as exc:
            ctx.check()
            raise RuntimeError(
                "R2 업로드 또는 공개 URL 확인에 실패했습니다. 연결 설정과 인터넷을 확인해 주세요."
            ) from exc

    def remove_media(self, key):
        client, c = self.r2()
        try:
            client.delete_object(Bucket=c["R2_BUCKET"], Key=key)
            try:
                client.head_object(Bucket=c["R2_BUCKET"], Key=key)
            except client.exceptions.ClientError as error:
                if (
                    error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                    != 404
                ):
                    raise
            else:
                raise RuntimeError("삭제된 객체가 아직 조회됩니다.")
        except Exception as exc:
            raise RuntimeError(
                "R2 영상 정리에 실패했습니다. 다음 확인 때 다시 시도합니다."
            ) from exc


class Publisher:
    def __init__(self, store, jobs, connections):
        self.store, self.jobs, self.connections = store, jobs, connections
        self.path = store.root / "deliveries.json"
        self.lock = threading.RLock()
        if not self.path.exists():
            atomic_json(self.path, [])
        # Crash during create means unknown; do not blindly recreate.
        records = self.records()
        for r in records:
            for d in r["deliveries"]:
                if d["status"] == "creating":
                    d["status"] = "unknown"
        self.save(records)
        jobs.handlers["schedule"] = self.schedule
        jobs.handlers["refresh"] = self.refresh_job
        jobs.handlers["delete_reservation"] = self.delete_job

    def records(self):
        with self.lock:
            return json.loads(self.path.read_text())

    def save(self, records):
        with self.lock:
            atomic_json(self.path, records)

    def schedule(self, ctx, pid, args):
        p = self.store.load(pid)
        explicit = args.get("schedule")
        due = timestamp(explicit[0]["due_at"] if explicit else args["due_at"])
        if due < datetime.now(timezone.utc) + timedelta(minutes=10):
            raise ValueError("예약은 현재 시각보다 최소 10분 뒤로 지정해 주세요.")
        channel_ids = args.get("channel_ids", [])
        channels = [c for c in self.connections.channels() if c["id"] in channel_ids]
        if not channels or len(channels) != len(set(channel_ids)):
            raise ValueError("인스타그램·유튜브·틱톡 채널을 선택해 주세요.")
        if any(c["isQueuePaused"] for c in channels):
            raise ValueError("선택한 Buffer 채널의 대기열이 일시정지 상태입니다.")
        clips = [
            c
            for c in p["clips"]
            if c["included"] and c["id"] in args.get("clip_ids", [])
        ]
        if not clips:
            raise ValueError("예약할 쇼츠를 선택해 주세요.")
        spacing = int(args.get("spacing_minutes", 60))
        if spacing < 1:
            raise ValueError("영상 간 예약 간격은 최소 1분입니다.")
        privacy = args.get("youtube_privacy", "public")
        if privacy not in ("public", "private", "unlisted"):
            raise ValueError("YouTube 공개 범위를 확인해 주세요.")
        if explicit is not None:
            dates = {row["clip_id"]: timestamp(row["due_at"]).isoformat() for row in explicit}
            if len(dates) != len(explicit) or set(dates) != {c["id"] for c in clips}:
                raise ValueError("예약 달력과 선택 영상이 일치하지 않습니다.")
        else:
            dates = {c["id"]: (due + timedelta(minutes=spacing * i)).isoformat()
                     for i, c in enumerate(clips)}
        if any(timestamp(t) < datetime.now(timezone.utc) + timedelta(minutes=10) for t in dates.values()):
            raise ValueError("예약 시각이 지났거나 너무 임박했습니다. 달력을 다시 확인해 주세요.")
        records = self.records()
        for i, clip in enumerate(clips):
            ctx.check()
            render = clip.get("render")
            key = render_key(p, clip)
            if (
                not clip.get("confirmed")
                or not render
                or render["key"] != key
                or not Path(render["path"]).is_file()
            ):
                raise ValueError(
                    "현재 편집 내용으로 인코딩을 완료한 쇼츠만 예약할 수 있습니다."
                )
            scheduled = dates[clip["id"]]
            if any(
                r["project_id"] == pid
                and r["clip_id"] == clip["id"]
                and r["render_key"] != key
                and not r.get("deleted_at")
                for r in records
            ):
                raise ValueError(
                    "이 쇼츠의 이전 편집본 예약이 남아 있습니다. 기존 예약을 취소한 뒤 새 편집본을 예약해 주세요."
                )
            record = next(
                (
                    r
                    for r in records
                    if r["project_id"] == pid
                    and r["clip_id"] == clip["id"]
                    and r["render_key"] == key
                    and not r.get("deleted_at")
                ),
                None,
            )
            if record:
                existing = {d["channel_id"]: d for d in record["deliveries"]}
                for channel in channels:
                    d = existing.get(channel["id"])
                    if d and d["status"] not in ("pending", "rejected", "cancelled"):
                        if d["due_at"] != scheduled:
                            raise ValueError(
                                "이미 예약된 쇼츠입니다. 기존 예약을 삭제한 뒤 시간을 바꿔 주세요."
                            )
                        if d["status"] == "unknown":
                            raise ValueError(
                                "이전 예약의 성공 여부가 불명확합니다. Buffer에서 확인하고 게시물 ID를 연결해 주세요. 중복 예약은 만들지 않았습니다."
                            )
            else:
                record = dict(
                    id=uid(),
                    project_id=pid,
                    clip_id=clip["id"],
                    title=clip["hook"],
                    render_key=key,
                    object_key=f"shorts/{pid}/{uid()}.mp4",
                    url=None,
                    created_at=now(),
                    deliveries=[],
                )
                records.append(record)
                self.save(records)
            # Persist every selected destination before upload/create. A partial failure must
            # never make cleanup mistake one successful channel for the entire request.
            for channel in channels:
                if not any(
                    d["channel_id"] == channel["id"] for d in record["deliveries"]
                ):
                    record["deliveries"].append(
                        dict(
                            channel_id=channel["id"],
                            service=channel["service"],
                            channel_name=channel["displayName"] or channel["name"],
                            status="pending",
                            due_at=scheduled,
                            post_id=None,
                            sent_at=None,
                            error=None,
                        )
                    )
            self.save(records)
            if not record["url"]:
                record["url"] = self.connections.upload(
                    render["path"], record["object_key"], ctx
                )
                self.save(records)
            for channel in channels:
                ctx.check()
                d = next(
                    (
                        d
                        for d in record["deliveries"]
                        if d["channel_id"] == channel["id"]
                    ),
                    None,
                )
                if d and d["status"] not in ("pending", "rejected", "cancelled"):
                    continue
                if d is None:
                    d = dict(
                        channel_id=channel["id"],
                        service=channel["service"],
                        channel_name=channel["displayName"] or channel["name"],
                    )
                    record["deliveries"].append(d)
                d.update(
                    status="creating",
                    due_at=scheduled,
                    post_id=None,
                    sent_at=None,
                    error=None,
                )
                self.save(records)  # Durable intent BEFORE mutation.
                ctx.progress(f"{channel['service']} 예약 중 · {clip['hook']}")
                try:
                    post = self.connections.create(
                        channel, clip["hook"], scheduled, record["url"], privacy
                    )
                    d.update(
                        post_id=post["id"],
                        status=post["status"],
                        sent_at=post.get("sentAt"),
                    )
                    self.save(records)
                except BufferError as exc:
                    d.update(
                        status="rejected" if exc.code == "REJECTED" else "unknown",
                        error=str(exc),
                    )
                    self.save(records)
                    raise
                # Persist ID before checking cancellation or fetching status.
                ctx.check()
                checked = self.connections.post(d["post_id"])
                d.update(
                    status=checked["status"],
                    sent_at=checked.get("sentAt"),
                    checked_at=now(),
                )
                self.save(records)

    def refresh_job(self, ctx, pid, args):
        self.refresh(ctx)

    def refresh(self, ctx=None):
        # Jobs serialize mutations; also lock for manual reconciliation/background refresh.
        with self.lock:
            records = self.records()
            for record in records:
                if record.get("deleted_at"):
                    continue
                for d in record["deliveries"]:
                    if ctx:
                        ctx.check()
                    if not d.get("post_id") or d["status"] == "cancelled":
                        continue
                    try:
                        post = self.connections.post(d["post_id"])
                        d.update(
                            status=post["status"],
                            sent_at=post.get("sentAt"),
                            external_link=post.get("externalLink"),
                            checked_at=now(),
                            error=None,
                        )
                    except BufferError as exc:
                        d.update(status="unknown", error=str(exc), checked_at=now())
                    self.save(records)
                if cleanup_due(record):
                    self.connections.remove_media(record["object_key"])
                    record["deleted_at"] = now()
                    record["deletion_reason"] = "모든 채널 발행 성공 후 14일 경과"
                    self.save(records)

    def delete_job(self, ctx, pid, args):
        with self.lock:
            records = self.records()
            record = next(
                r
                for r in records
                if r["id"] == args["record_id"] and r["project_id"] == pid
            )
            for d in record["deliveries"]:
                ctx.check()
                if d["status"] == "unknown" and not d.get("post_id"):
                    raise ValueError(
                        "불명확한 예약은 먼저 Buffer 게시물 ID를 연결해 주세요. 원격 영상은 보존했습니다."
                    )
                if d.get("post_id") and d["status"] != "cancelled":
                    try:
                        post = self.connections.post(d["post_id"])
                    except BufferError as exc:
                        if exc.code != "NOT_FOUND":
                            raise
                        d["status"] = "cancelled"
                        self.save(records)
                        continue
                    if post["status"] in ("sending", "sent"):
                        raise ValueError(
                            "발행 중이거나 발행된 게시물은 여기서 삭제하지 않습니다."
                        )
                    self.connections.delete(d["post_id"])
                    try:
                        self.connections.post(d["post_id"])
                    except BufferError as exc:
                        if exc.code != "NOT_FOUND":
                            raise
                    else:
                        raise RuntimeError(
                            "예약 삭제 확인에 실패했습니다. 원격 영상을 보존합니다."
                        )
                    d["status"] = "cancelled"
                    self.save(records)
            if all(
                d["status"] in ("pending", "cancelled", "rejected")
                for d in record["deliveries"]
            ):
                self.connections.remove_media(record["object_key"])
                record.update(deleted_at=now(), deletion_reason="사용자가 예약 취소")
                self.save(records)

    def reconcile(self, pid, record_id, channel_id, post_id):
        with self.lock:
            records = self.records()
            record = next(
                r for r in records if r["id"] == record_id and r["project_id"] == pid
            )
            delivery = next(
                d for d in record["deliveries"] if d["channel_id"] == channel_id
            )
            if delivery["status"] != "unknown":
                raise ValueError("상태가 불명확한 예약만 연결할 수 있습니다.")
            post = self.connections.post(post_id)
            if (
                post["channelId"] != channel_id
                or post["text"] != record["title"]
                or not post.get("dueAt")
                or abs(
                    (
                        timestamp(post["dueAt"]) - timestamp(delivery["due_at"])
                    ).total_seconds()
                )
                > 2
            ):
                raise ValueError(
                    "해당 게시물의 채널·문구·예약 시간이 작업 기록과 다릅니다."
                )
            delivery.update(
                post_id=post_id,
                status=post["status"],
                sent_at=post.get("sentAt"),
                error=None,
                checked_at=now(),
            )
            self.save(records)
