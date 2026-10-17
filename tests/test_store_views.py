"""Read-only media requests reuse current state without loading undo snapshots."""

import copy
import os
from pathlib import Path

import pytest
from PIL import Image

from app import create_app
from shortsmaker import media
from shortsmaker.service import new_clip
from shortsmaker.store import Store


def populated_store(tmp_path):
    store = Store(tmp_path)
    project = store.create('/tmp/source.mp4', dict(duration=10, width=1920, height=1080))
    clip = new_clip(0, 10)
    clip.update(hook='기존 문구', source_shots=[dict(id='shot', start=0, end=10, frame_times=[0, .04, .08])])
    project = store.change(project['id'], lambda p: p.update(clips=[clip], history=[[copy.deepcopy(clip)]]))
    return store, project


def test_views_are_independent_and_leave_complete_history_available(tmp_path):
    store, project = populated_store(tmp_path)
    first = store.load(project['id'], include_history=False)
    assert 'history' not in first
    first['clips'][0]['source_shots'][0]['frame_times'].append(99)
    first['metadata']['duration'] = 99
    first.pop('clips')
    assert store.load(project['id'], include_history=False) == {k: v for k, v in project.items() if k != 'history'}
    assert store.load(project['id']) == project
    assert store.list() == [project]
    assert 'history' not in store.list(include_history=False)[0]


def test_cached_views_do_not_reread_json_and_detect_store_edits(tmp_path, monkeypatch):
    store, project = populated_store(tmp_path)
    path = store.folder(project['id']) / 'project.json'
    first = store.load(project['id'], include_history=False)
    original_open = Path.open

    def no_project_read(self, *args, **kwargs):
        if self == path:
            pytest.fail('unchanged current-state reads should not open project JSON')
        return original_open(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', no_project_read)
        assert store.load(project['id'], include_history=False) == first
        assert store.list(include_history=False) == [first]
    updated = store.change(project['id'], lambda p: p['clips'][0].update(hook='수정 문구'), history=True)
    assert store.load(project['id'], include_history=False)['clips'][0]['hook'] == '수정 문구'
    assert store.load(project['id']) == updated
    assert updated['history'][-1] == project['clips']


@pytest.mark.parametrize('atomic_replace', [False, True])
def test_external_changes_with_same_size_and_mtime_invalidate_views(tmp_path, atomic_replace):
    store, project = populated_store(tmp_path)
    path = store.folder(project['id']) / 'project.json'
    original = store.load(project['id'], include_history=False)
    before = path.stat()
    # Same-length text keeps the file size identical; preserve mtime to ensure
    # inode/ctime catch both atomic writers and edits outside this Store.
    text = path.read_text().replace('기존 문구', '외부 수정')
    if atomic_replace:
        replacement = path.with_suffix('.replacement')
        replacement.write_text(text)
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(path)
    else:
        path.write_text(text)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    after = store.load(project['id'], include_history=False)
    assert after['clips'][0]['hook'] == '외부 수정'
    assert original['clips'][0]['hook'] == '기존 문구'


def test_list_views_detect_external_add_delete_and_hide_archived_projects(tmp_path):
    store, first = populated_store(tmp_path)
    assert len(store.list(include_history=False)) == 1
    other_writer = Store(tmp_path)
    second = other_writer.create('/tmp/second.mp4', dict(duration=10))
    assert {p['id'] for p in store.list(include_history=False)} == {first['id'], second['id']}
    other_writer.delete(first['id'])
    assert [p['id'] for p in store.list(include_history=False)] == [second['id']]
    (other_writer.folder(second['id']) / 'project.json').unlink()
    assert store.list(include_history=False) == []
    with pytest.raises(FileNotFoundError):
        store.load(second['id'], include_history=False)


def test_state_and_media_routes_use_cached_views_while_undo_stays_complete(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'data')
    app.config.update(TESTING=True)
    store = app.extensions['store']
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'0123456789abcdef')
    project = store.create(source, dict(duration=10, width=1920, height=1080))
    clip = new_clip(0, 10)
    clip.update(hook='제목', source_shots=[dict(id='shot', start=0, end=10, thumbnail='shot.jpg')],
                frame_suggestions=[dict(id='info', thumbnail='info.jpg')],
                render=dict(key='test-render', path=str(source)))
    project = store.change(project['id'], lambda p: p.update(clips=[clip], history=[[copy.deepcopy(clip)]]))
    folder = store.folder(project['id'])
    (folder / 'titles').mkdir()
    Image.new('RGBA', (4, 4)).save(folder / 'titles' / 'test-render.png')
    frame_folder = folder / 'frames' / clip['id']
    frame_folder.mkdir(parents=True)
    for name in ('shot.jpg', 'info.jpg', 'timeline.jpg'):
        Image.new('RGB', (4, 4)).save(frame_folder / name)
    monkeypatch.setattr(media, 'render_key', lambda *args: 'test-render')
    monkeypatch.setattr(media, 'timeline_thumbnail', lambda *args: frame_folder / 'timeline.jpg')
    monkeypatch.setattr(app.extensions['service'], 'source', lambda p: source)
    store.load(project['id'], include_history=False)
    path, original_open = folder / 'project.json', Path.open

    def no_project_read(self, *args, **kwargs):
        if self == path:
            pytest.fail('media/state routes should reuse the warmed current state')
        return original_open(self, *args, **kwargs)

    client = app.test_client()
    base = f'/api/projects/{project["id"]}'
    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, 'open', no_project_read)
            for url in ('/api/state', base + '/source', base + '/timeline.jpg?time=0',
                        base + f'/clips/{clip["id"]}/title.png',
                        base + f'/clips/{clip["id"]}/shots/shot.jpg',
                        base + f'/clips/{clip["id"]}/information/info.jpg',
                        base + f'/clips/{clip["id"]}/output'):
                response = client.get(url)
                assert response.status_code == 200, url
                response.close()
        # A state response adds render_current and removes history only from its
        # returned copy. Editing and undo still read complete persisted snapshots.
        assert store.load(project['id']) == project
        headers = {'X-Shortsmaker': '1'}
        changed = client.post(base + '/edit', headers=headers,
                              json=dict(action='update', clip_id=clip['id'], changes=dict(hook='수정 제목')))
        assert changed.status_code == 200
        assert client.get('/api/state').json['projects'][0]['clips'][0]['hook'] == '수정 제목'
        restored = client.post(base + '/edit', headers=headers, json=dict(action='undo'))
        assert restored.status_code == 200
        assert client.get('/api/state').json['projects'][0]['clips'] == restored.json['clips']
        assert store.load(project['id'])['clips'] == project['clips']
        assert store.load(project['id'])['history'] == project['history']
    finally:
        app.extensions['jobs'].shutdown()
