from copy import deepcopy
from pathlib import Path

import pytest
from PIL import Image

from app import create_app
from shortsmaker import media
from shortsmaker.service import new_clip


@pytest.fixture
def setup(tmp_path):
    app = create_app(tmp_path / 'data')
    app.config['TESTING'] = True
    source = tmp_path / 'original.mp4'
    source.write_bytes(b'original')
    stat = source.stat()
    meta = dict(size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                width=1920, height=1080, duration=60, fps='30/1', has_audio=True, hdr=False)
    store = app.extensions['store']
    p = store.create(source, meta)
    c = new_clip(0, 30, '테스트')
    c.update(hook='기존 후킹 문구', yellow='기존')
    p = store.change(p['id'], lambda p: p.update(clips=[c]))
    return app, p, source


def test_title_changes_and_corrupt_cache_recovers(setup):
    app, p, _ = setup
    client = app.test_client()
    c = p['clips'][0]
    url = f"/api/projects/{p['id']}/clips/{c['id']}/title.png"
    first = client.get(url)
    assert first.status_code == 200
    updated = client.post(f"/api/projects/{p['id']}/edit", json={
        'action': 'update', 'revision': p['revision'], 'clip_id': c['id'],
        'changes': {'hook': '직접 수정한 문구\n미리보기 반영', 'yellow': '미리보기'}
    }, headers={'X-Shortsmaker': '1'})
    assert updated.status_code == 200
    second = client.get(url)
    assert second.status_code == 200 and second.data != first.data
    p = updated.json
    cached = app.extensions['store'].folder(p['id']) / 'titles' / (media.render_key(p, p['clips'][0]) + '.png')
    cached.write_bytes(b'partial png')
    repaired = client.get(url)
    assert repaired.status_code == 200 and repaired.data == second.data
    with Image.open(cached) as img:
        img.verify()


def test_unreadable_font_falls_back(tmp_path, monkeypatch):
    valid = Path(media.title_font(40).path)
    broken = tmp_path / 'copied.otf'
    broken.write_bytes(b'not a font')
    monkeypatch.setattr(media, 'FONT_CANDIDATES', [broken, valid])
    media.title_image('한글 표시 확인', '한글', tmp_path / 'title.png')
    assert Path(media.title_font(40).path) == valid
    assert media.font_key() != 'unavailable'


def test_legacy_timestamp_precision_requires_media_match(setup, monkeypatch):
    app, p, source = setup
    service = app.extensions['service']
    original = deepcopy(p['metadata'])
    p['metadata']['mtime_ns'] = source.stat().st_mtime_ns // 1_000_000_000 * 1_000_000_000
    monkeypatch.setattr(media, 'probe', lambda _: dict(original, width=1280))
    with pytest.raises(ValueError, match='다시 연결'):
        service.source(p)
    monkeypatch.setattr(media, 'probe', lambda _: original)
    assert service.source(p) == source
    p['metadata']['mtime_ns'] -= 1_000_000_000
    with pytest.raises(ValueError, match='다시 연결'):
        service.source(p)


def test_reconnect_preserves_edits_and_rejects_other_video(setup, monkeypatch, tmp_path):
    app, p, source = setup
    service = app.extensions['service']
    moved = tmp_path / 'moved.mp4'
    moved.write_bytes(source.read_bytes())
    monkeypatch.setattr(media, 'probe', lambda _: dict(p['metadata'], width=1280))
    with pytest.raises(ValueError):
        service.reconnect(p['id'], moved)
    assert app.extensions['store'].load(p['id']) == p
    monkeypatch.setattr(media, 'probe', lambda _: dict(p['metadata'], mtime_ns=moved.stat().st_mtime_ns))
    result = service.reconnect(p['id'], moved)
    assert result['clips'] == p['clips']
    assert result['source'] == str(moved)
    assert service.source(result) == moved


def test_missing_information_cache_rebuilds_at_saved_time(setup, monkeypatch):
    app, p, source = setup
    c = p['clips'][0]
    suggestion = dict(id='saved-frame', time=12.5, thumbnail='frame-001.jpg')
    app.extensions['store'].change(p['id'], lambda p: p['clips'][0].update(frame_suggestions=[suggestion]))
    calls = []
    def rebuild(path, at, target):
        calls.append((path, at))
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (20, 10), 'blue').save(target)
    monkeypatch.setattr(media, 'information_image', rebuild)
    url = f"/api/projects/{p['id']}/clips/{c['id']}/information/saved-frame.jpg"
    client = app.test_client()
    assert client.get(url).status_code == 200
    assert client.get(url).status_code == 200
    assert calls == [(source, 12.5)]


def test_missing_fonts_do_not_break_state(setup, monkeypatch):
    app, p, _ = setup
    monkeypatch.setattr(media, 'FONT_CANDIDATES', [])
    assert app.test_client().get('/api/state').status_code == 200
    c = p['clips'][0]
    result = app.test_client().get(f"/api/projects/{p['id']}/clips/{c['id']}/title.png")
    assert result.status_code == 400 and '한글 폰트' in result.json['error']
