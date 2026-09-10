# Shortsmaker

편집한 가로 롱폼을 주제별 쇼츠로 만들고 Instagram·YouTube에 예약하는 Mac 로컬 앱.

## 실행

`시작.command`를 더블클릭하면 `http://127.0.0.1:5099`가 열립니다. 터미널을 닫으면 서버가 종료됩니다.

필요 환경: Python 3.14, FFmpeg/ffprobe, Codex CLI의 ChatGPT 구독 로그인. 자막 영역 인식은 Mac Vision과 Xcode Command Line Tools의 Swift를 사용합니다. 첫 실행 시 Python 패키지와 음성 인식 모델을 다운로드합니다. GmarketSansBold가 설치되어 있으면 사용하며 없으면 Mac 기본 한글 폰트로 표시합니다.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

## 작업 흐름

1. 편집한 가로 원본 불러오기 → 내용 분석·분할. 전사는 분석용이며 새 자막은 넣지 않습니다.
2. AI 구간을 검토하고 시작/끝 변경·분할·합치기·제외.
3. 후킹 10개 중 선택, 강조 구절과 문구 수정 후 확정.
4. 자동 구도 분석 → 미리보기에서 기존 자막과 중요 대상 확인. 직접 위치·확대율 조정 가능.
5. 포함한 쇼츠 인코딩 → 완성 영상 확인.
6. 채널·예약 시각·간격 선택 → R2 업로드 및 Buffer 예약.

기본 AI는 `gpt-6-astra` / `medium`, 설정에서 변경할 수 있습니다. 음성 인식은 로컬 faster-whisper medium입니다. AI는 전사와 대표 프레임을 사용하므로 구독 사용량을 소비합니다.

## 파일과 복구

- 원본 영상은 수정하지 않습니다. 프로젝트·전사·AI 응답·완성본은 `data/projects/`에 저장됩니다. 앱의 폴더 열기는 프로젝트별 `완성 영상/` 폴더를 엽니다.
- 각 단계 완료 내용을 저장하고 앱 재시작 후 중단 작업은 재시도할 수 있습니다.
- 편집 변경 시 이전 출력은 남겨두고 새 출력만 만듭니다. 현재 내용과 다른 출력은 예약에 사용하지 않습니다.
- R2의 예약용 복사본은 선택한 모든 채널에서 발행 성공한 뒤 14일 후 정리합니다. 앱 시작 및 실행 중 매시간 확인하며, 앱이 꺼져 있으면 다음 실행 때 처리합니다.
- 예약/오류/상태 불명 영상은 자동 삭제하지 않습니다. Mac 원본·완성본은 자동 삭제하지 않습니다.
- 예약 응답이 유실되면 중복 방지를 위해 자동 재생성을 멈춥니다. Buffer에서 확인한 게시물 ID를 연결한 후 처리합니다.
- `.env.local`에 `.env.example`의 Buffer/R2 설정을 입력합니다. 비밀값과 영상은 Git에 올리지 않습니다.

## 개발 검증

```sh
.venv/bin/python -m pytest -q
node --check static/app.js
```

진행 상황과 검증 한계는 `docs/verification/initial-version.md`에 기록합니다. 개인 실사용 베타 구현 및 두 원본16개 쇼츠 검증을 완료했습니다. 실제 플랫폼 발행 시험은 하지 않았습니다.
