"""Scene cuts + multimodal important-region estimates; conservative stable crop."""
import math
from pathlib import Path
import cv2
from PIL import Image,ImageDraw
from . import ai


def sample_scenes(ctx,source,clip,folder):
    folder=Path(folder)
    folder.mkdir(parents=True,exist_ok=True)
    cap=cv2.VideoCapture(source)
    if not cap.isOpened():
        raise ValueError('구도 분석용 영상을 열 수 없습니다.')
    cuts=[clip['start']]
    previous=None
    try:
        # Inspect every second for cuts. Color-distribution plus pixel changes avoid most motion cuts.
        t=clip['start']
        while t<clip['end']:
            ctx.check()
            cap.set(cv2.CAP_PROP_POS_MSEC,t*1000)
            ok,frame=cap.read()
            if not ok:
                t+=1
                continue
            small=cv2.resize(frame,(160,90))
            hist=cv2.calcHist([small],[0,1,2],None,[8,8,8],[0,256]*3)
            cv2.normalize(hist,hist)
            if previous is not None:
                diff=cv2.compareHist(previous[0],hist,cv2.HISTCMP_BHATTACHARYYA)
                motion=cv2.absdiff(previous[1],small).mean()
                if diff>.48 and motion>35 and t-cuts[-1]>=2:
                    cuts.append(t)
            previous=(hist,small)
            t+=1
        cuts.append(clip['end'])
        scenes=[{'start':a,'end':b} for a,b in zip(cuts,cuts[1:]) if b>a]
        frames=[]
        for scene_i,scene in enumerate(scenes):
            length=scene['end']-scene['start']
            # Every ~12s, with both ends represented to protect movement/subtitle variation.
            count=max(3,min(16,math.ceil(length/12)+1))
            for j in range(count):
                t=scene['start']+min(length-.05,max(.05,length*j/(count-1)))
                cap.set(cv2.CAP_PROP_POS_MSEC,t*1000)
                ok,frame=cap.read()
                if not ok:
                    continue
                path=folder/f'frame-{len(frames):03}.jpg'
                frame=cv2.cvtColor(frame,cv2.COLOR_BGR2RGB)
                image=Image.fromarray(frame)
                image.thumbnail((960,540))
                image.save(path,quality=88)
                frames.append(dict(index=len(frames),scene=scene_i,time=t,path=str(path)))
        return scenes,frames
    finally:
        cap.release()


def analyze(ctx,project,clip,folder,cache_dir):
    scenes,frames=sample_scenes(ctx,project['source'],clip,folder)
    regions={}
    for offset in range(0,len(frames),6):
        group=frames[offset:offset+6]
        sheet=Image.new('RGB',(1920,600*math.ceil(len(group)/2)), '#121212')
        draw=ImageDraw.Draw(sheet)
        for i,f in enumerate(group):
            x,y=(i%2)*960,(i//2)*600
            sheet.paste(Image.open(f['path']),(x,y+40))
            draw.text((x+15,y+10),f"FRAME {f['index']} / {f['time']:.2f}s",fill='white')
        sheet_path=Path(folder)/f'sheet-{offset}.jpg'
        sheet.save(sheet_path,quality=90)
        ctx.progress(f"구도 분석 · 프레임 {offset+1}~{offset+len(group)}/{len(frames)}")
        context=' '.join(s['text'] for s in project['transcript'] if s['end']>clip['start'] and s['start']<clip['end'])
        prompt=f'''첨부는 원본 가로 영상의 시간별 프레임 모음입니다. 각 프레임의 전체 가로폭을 0~1로 보세요.
쇼츠 크롭에서 보존해야 할 중요 영역의 왼쪽 left, 오른쪽 right를 반환하세요.
얼굴/몸/동작하는 손, 설명 중인 판서/물건/동작, 특히 화면 하단 기존 자막의 전체 글자 폭을 함께 포함하세요.
자막을 재생성하지 않으므로 한 글자라도 자르면 안 됩니다. 관련 없는 배경까지 포함할 필요는 없습니다.
불확실하면 넓게 잡고 confidence를 낮추세요. 여백 조금 포함, 0<=left<right<=1. confidence는 0~1.
각 FRAME 번호마다 정확히 하나 반환하고 한국어로 이유를 간결하게 쓰세요.
FRAME 번호: {[f['index'] for f in group]}\n발언 데이터: {context}'''
        result=ai.call(ctx,prompt,ai.FRAME_SCHEMA,cache_dir,project['model'],project['effort'],images=[sheet_path])
        expected={f['index'] for f in group}
        if {r['index'] for r in result['frames']}!=expected or len(result['frames'])!=len(group):
            raise ValueError('AI 구도 응답의 프레임 개수가 일치하지 않습니다. 다시 시도해 주세요.')
        for r in result['frames']:
            if not all(math.isfinite(r[k]) for k in ('left','right','confidence')) or not 0<=r['left']<r['right']<=1 or not 0<=r['confidence']<=1:
                raise ValueError('AI 구도 좌표가 올바르지 않습니다.')
            regions[r['index']]=r
    result=[]
    for i,scene in enumerate(scenes):
        rs=[regions[f['index']] for f in frames if f['scene']==i]
        if not rs:
            result.append(dict(scene,zoom=1,center=.5,review=True,reason='프레임 읽기 실패 · 전체 폭으로 보존'))
            continue
        left=max(0,min(r['left'] for r in rs)-.025)
        right=min(1,max(r['right'] for r in rs)+.025)
        zoom=min(1.5,1/max(right-left,.01))
        result.append(dict(scene,zoom=round(zoom,4),center=round((left+right)/2,4),
                           review=min(r['confidence'] for r in rs)<.8,
                           reason='장면 내 중요 영역과 기존 자막을 함께 보존. '+rs[len(rs)//2]['reason']))
    return result
