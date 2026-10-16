from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import threading
import time

import cv2
import numpy as np
from PIL import Image
import pytest

from app import create_app
from shortsmaker import media


@pytest.fixture
def timeline(tmp_path):
    source = tmp_path / 'source.avi'
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*'MJPG'), 10, (640, 360))
    assert writer.isOpened()
    for index in range(6):
        frame = np.empty((360, 640, 3), dtype=np.uint8)
        frame[:] = (index * 30, 0, 250 - index * 30)
        writer.write(frame)
    writer.release()
    app = create_app(tmp_path / 'data')
    app.config.update(TESTING=True)
    store = app.extensions['store']
    stat = source.stat()
    project = store.create(source, dict(duration=.6, fps='10/1', width=640, height=360,
                                        size=stat.st_size, mtime_ns=stat.st_mtime_ns))
    yield app.test_client(), store, project
    app.extensions['jobs'].shutdown()


def test_timeline_images_include_end_frame_and_leave_project_untouched(timeline):
    client, store, project = timeline
    saved = (store.folder(project['id']) / 'project.json').read_bytes()
    pixels = []
    for at in (0, .3, .6):
        response = client.get(f"/api/projects/{project['id']}/timeline.jpg?time={at}")
        assert response.status_code == 200 and response.mimetype == 'image/jpeg'
        image = Image.open(BytesIO(response.data))
        assert image.width <= 320 and image.height <= 180
        pixels.append(image.getpixel((0, 0)))
    assert pixels[0][0] > pixels[-1][0] + 100
    assert pixels[-1][2] > pixels[0][2] + 100
    assert (store.folder(project['id']) / 'project.json').read_bytes() == saved
    assert store.load(project['id']) == project


@pytest.mark.parametrize('value', [None, '', '-0.01', '0.601', 'NaN', 'inf', '-inf', 'oops'])
def test_invalid_timeline_times_are_rejected_without_writes(timeline, value):
    client, store, project = timeline
    query = '' if value is None else f'?time={value}'
    response = client.get(f"/api/projects/{project['id']}/timeline.jpg{query}")
    assert response.status_code == 400
    assert store.load(project['id']) == project
    assert not (store.folder(project['id']) / 'timeline-thumbnails').exists()


def test_thumbnail_cache_survives_project_revision_changes(timeline, monkeypatch):
    client, store, project = timeline
    original = media._timeline_frame
    calls = []

    def decode(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(media, '_timeline_frame', decode)
    url = f"/api/projects/{project['id']}/timeline.jpg?time=0.2"
    first = client.get(url)
    changed = store.change(project['id'], lambda p: p.update(name='새 이름'))
    second = client.get(url + '00000')
    assert first.status_code == second.status_code == 200
    assert first.data == second.data
    assert first.headers['ETag'] == second.headers['ETag']
    assert len(calls) == 1
    assert store.load(project['id']) == changed


def test_source_stat_changes_invalidate_thumbnail_cache(tmp_path, monkeypatch):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'old')
    monkeypatch.setattr(media, '_timeline_frame', lambda *args: Image.new('RGB', (640, 360)))
    metadata = dict(duration=10, fps='30/1')
    first = media.timeline_thumbnail(source, 2, metadata, tmp_path / 'cache')
    source.write_bytes(b'new source content')
    second = media.timeline_thumbnail(source, 2, metadata, tmp_path / 'cache')
    assert first != second
    assert first.is_file() and second.is_file()


def test_concurrent_requests_share_cache_and_limit_decoders(tmp_path, monkeypatch):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'source')
    lock = threading.Lock()
    active, peak, calls = 0, 0, []

    def decode(source, at, duration, fps):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            calls.append(at)
        try:
            time.sleep(.03)
            return Image.new('RGB', (640, 360))
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(media, '_timeline_frame', decode)
    values = [1, 1, 2, 3, 4, 1, 2, 5]
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(lambda at: media.timeline_thumbnail(
            source, at, dict(duration=10), tmp_path / 'cache'), values))
    assert peak <= 2
    assert sorted(calls) == [1, 2, 3, 4, 5]
    assert len(set(paths)) == 5
    assert len(list((tmp_path / 'cache').iterdir())) == 5
    assert all(Image.open(path).size == (320, 180) for path in paths)


def test_failed_thumbnail_write_is_not_cached(tmp_path, monkeypatch):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'source')
    monkeypatch.setattr(media, '_timeline_frame', lambda *args: Image.new('RGB', (320, 180)))

    def fail(*args, **kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(Image.Image, 'save', fail)
    with pytest.raises(OSError, match='disk full'):
        media.timeline_thumbnail(source, 1, dict(duration=10), tmp_path / 'cache')
    assert list((tmp_path / 'cache').iterdir()) == []
