from pathlib import Path
from types import SimpleNamespace
import pytest
from app import create_app
from shortsmaker.service import Service, new_clip, export_folder
from shortsmaker.store import Store
from shortsmaker import media


def test_export_requires_folder_and_copies_to_selected_location(tmp_path, monkeypatch):
    store=Store(tmp_path/'data');p=store.create('/tmp/source.mp4',dict(duration=20))
    c=new_clip(0,10);p=store.change(p['id'],lambda p:p.update(clips=[c]))
    service=Service(store,SimpleNamespace(handlers={}))
    monkeypatch.setattr(service,'source',lambda p:None)
    with pytest.raises(ValueError,match='저장 폴더'):service.encode(None,p['id'],{})
    destination=tmp_path/'내 완성 영상';destination.mkdir()
    cached=tmp_path/'cached.mp4';cached.write_bytes(b'encoded-output')
    monkeypatch.setattr(media,'export_clip',lambda *args:dict(path=str(cached),key='abcdef123456'))
    store.change(p['id'],lambda p:p.update(export_dir=str(destination)))
    service.encode(None,p['id'],{})
    result=store.load(p['id'])['clips'][0]['render']
    assert Path(result['export_path']).parent==destination
    assert Path(result['export_path']).read_bytes()==b'encoded-output'
    destination.rename(tmp_path/'moved')
    with pytest.raises(ValueError,match='다시 설정'):service.encode(None,p['id'],{})


def test_folder_picker_cancel_selection_and_encode_gate(tmp_path, monkeypatch):
    app=create_app(tmp_path/'data');client=app.test_client();store=app.extensions['store']
    p=store.create('/tmp/source.mp4',dict(duration=20));headers={'X-Shortsmaker':'1'}
    route=f"/api/projects/{p['id']}"
    assert client.post(route+'/jobs/encode',json={},headers=headers).status_code==400
    monkeypatch.setattr('app.subprocess.run',lambda *args,**kw:SimpleNamespace(returncode=1,stderr='User cancelled (-128)',stdout=''))
    assert client.post(route+'/export-folder',json={},headers=headers).json['cancelled']
    assert not store.load(p['id']).get('export_dir')
    folder=tmp_path/'선택 위치';folder.mkdir()
    monkeypatch.setattr('app.subprocess.run',lambda *args,**kw:SimpleNamespace(returncode=0,stderr='',stdout=str(folder)+'\n'))
    response=client.post(route+'/export-folder',json={},headers=headers)
    assert response.status_code==200 and response.json['export_dir']==str(folder)
    assert export_folder(store.load(p['id']))==folder
