"""Actual FFmpeg regression: old scene metadata cannot move the new fixed frame."""

import subprocess
import shutil
import numpy as np
import pytest
from shortsmaker.media import probe, export_clip, geometry
from shortsmaker.service import new_clip
from shortsmaker.jobs import Jobs, Context


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_fixed_render_retains_continuous_audio_timing(tmp_path):
    source = tmp_path / "source.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:r=30:d=5",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=sin(2*PI*440*t)*(0.5+0.45*sin(2*PI*3.7*t)):s=48000:d=5",
            "-c:v",
            "libx264",
            "-c:a",
            "pcm_s16le",
            "-shortest",
            str(source),
        ],
        check=True,
    )
    p = {"source": str(source), "metadata": probe(source)}
    c = new_clip(0.5, 3.5)
    c.update(
        hook="싱크 확인",
        yellow="싱크",
        confirmed=True,
        manual_frame=dict(zoom=1.5, center=0.3, vertical=0.6),
        frame_overrides=[dict(id="information",start=1.5,end=2.5,zoom=1.5,center=0.3,vertical=0.2)],
        framing=[
            dict(start=0.5 + i * 0.5, end=1 + i * 0.5, zoom=1.5, center=0.5)
            for i in range(6)
        ],
    )
    jobs = Jobs(tmp_path / "work")
    job = {"id": "test-render", "status": "running", "created_at": "now"}
    jobs.items[job["id"]] = job
    result = export_clip(Context(jobs, job), p, c, tmp_path)

    # The exported vertical position must match the preview geometry.
    frame_path = tmp_path / "frame.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", result["path"],
                    "-frames:v", "1", str(frame_path)], check=True)
    from PIL import Image
    pixels = np.asarray(Image.open(frame_path))
    blue = (pixels[:, :, 2] > 150) & (pixels[:, :, 0] < 80) & (pixels[:, :, 1] < 80)
    rows = np.where(blue[:, 540])[0]
    g = geometry(p["metadata"], **c["manual_frame"])
    assert abs(rows.min() - g["y"]) <= 2
    assert abs(rows.max() - (g["y"] + g["scaled_height"] - 1)) <= 2

    for at, vertical in [(1.5, .2), (2.5, .6)]:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(at), "-i", result["path"],
                        "-frames:v", "1", str(frame_path)], check=True)
        pixels = np.asarray(Image.open(frame_path))
        blue = (pixels[:, :, 2] > 150) & (pixels[:, :, 0] < 80) & (pixels[:, :, 1] < 80)
        rows = np.where(blue[:, 540])[0]
        assert abs(rows.min() - geometry(p['metadata'], vertical=vertical)['y']) <= 2

    def samples(path, start, duration):
        out = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                str(start),
                "-i",
                str(path),
                "-t",
                str(duration),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "2000",
                "-f",
                "f32le",
                "-",
            ],
            capture_output=True,
            check=True,
        )
        assert b"non monotonically" not in out.stderr
        a = np.frombuffer(out.stdout, dtype=np.float32)
        return np.sqrt(np.mean(a[: len(a) // 20 * 20].reshape(-1, 20) ** 2, axis=1))

    x = samples(source, 0.5, 3)
    y = samples(result["path"], 0, 3)
    # Late audio must stay aligned even when legacy scene metadata is present.
    at, size = 150, 100
    scores = [
        np.corrcoef(x[at + lag : at + lag + size], y[at : at + size])[0, 1]
        for lag in range(-15, 16)
    ]
    assert abs(int(np.argmax(scores)) - 15) <= 1
    assert max(scores) > 0.97


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
@pytest.mark.parametrize('has_audio', [True, False])
def test_internal_cuts_remove_video_and_audio_without_accumulating_delay(tmp_path, has_audio):
    from shortsmaker.timeline import kept_ranges, clip_duration
    source=tmp_path/'source.mkv'
    args=['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=blue:s=320x180:r=30:d=5']
    if has_audio:
        args += ['-f','lavfi','-i','aevalsrc=sin(2*PI*440*t)*(0.5+0.4*sin(2*PI*3.7*t)):s=48000:d=5','-c:a','pcm_s16le']
    subprocess.run(args+['-c:v','libx264','-t','5',str(source)],check=True)
    p=dict(source=str(source),metadata=probe(source))
    c=new_clip(.5,4.5)
    c.update(hook='삭제 확인',confirmed=True,
        cuts=[dict(start=1.07,end=1.83,reason='화면 조정'),dict(start=3.01,end=3.64,reason='반복')],
        frame_overrides=[dict(id='info',start=1.5,end=2.5,zoom=1.5,center=.5,vertical=.2)])
    jobs=Jobs(tmp_path/'work');job=dict(id='cut-render',status='running',created_at='now');jobs.items[job['id']]=job
    output=export_clip(Context(jobs,job),p,c,tmp_path)
    assert abs(output['metadata']['duration']-clip_duration(c)) < .05
    assert output['metadata']['has_audio']==has_audio
    # The retained source 1.86s now appears around output .60s with its source framing.
    frame=tmp_path/'frame.png'
    subprocess.run(['ffmpeg','-v','error','-y','-ss','0.6','-i',output['path'],'-frames:v','1',str(frame)],check=True)
    from PIL import Image
    pixels=np.asarray(Image.open(frame));blue=(pixels[:,:,2]>150)&(pixels[:,:,0]<80)&(pixels[:,:,1]<80)
    assert abs(np.where(blue[:,540])[0].min()-geometry(p['metadata'],vertical=.2)['y']) <=2
    if has_audio:
        def samples(path):
            r=subprocess.run(['ffmpeg','-v','error','-i',str(path),'-vn','-ac','1','-ar','8000','-f','f32le','-'],check=True,capture_output=True)
            assert b'non monotonically' not in r.stderr
            return np.frombuffer(r.stdout,dtype=np.float32)
        original=samples(source)
        expected=np.concatenate([original[round(r['start']*8000):round(r['end']*8000)] for r in kept_ranges(c)])
        actual=samples(output['path'])
        def envelope(a):return np.sqrt(np.mean(a[:len(a)//80*80].reshape(-1,80)**2,axis=1))
        x,y=envelope(expected),envelope(actual)
        size=min(len(x),len(y))-12
        scores=[np.corrcoef(x[5+lag:5+lag+size],y[5:5+size])[0,1] for lag in range(-4,5)]
        assert abs(int(np.argmax(scores))-4)<=1
        assert max(scores)>.98
    jobs.shutdown()
