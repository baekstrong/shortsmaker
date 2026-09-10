"""Isolated local transcription worker, killed safely with the job process group."""
import argparse
import json
from pathlib import Path
from .store import atomic_json


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('source')
    parser.add_argument('output')
    parser.add_argument('--model',default='medium',choices=['small','medium','large-v3'])
    args=parser.parse_args()
    from faster_whisper import WhisperModel
    print('음성 인식 모델 준비 중 · 첫 실행은 모델 다운로드가 필요합니다.',flush=True)
    model=WhisperModel(args.model,device='cpu',compute_type='int8',cpu_threads=6)
    segments,info=model.transcribe(args.source,language='ko',word_timestamps=True,vad_filter=True,
                                  initial_prompt='케틀벨 스윙, 근력, 체력, 운동 강도, 휴식, 반복, 세트')
    result=[]
    for seg in segments:
        result.append(dict(start=round(seg.start,3),end=round(seg.end,3),text=seg.text.strip(),
                           words=[dict(start=round(w.start,3),end=round(w.end,3),text=w.word.strip()) for w in seg.words or []]))
        print(json.dumps({'progress':min(99,100*seg.end/max(info.duration,1)),
                          'message':f'내용 분석용 음성 인식 {seg.end/60:.1f}/{info.duration/60:.1f}분'},ensure_ascii=False),flush=True)
    atomic_json(Path(args.output),result)


if __name__=='__main__':
    main()
