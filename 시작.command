#!/bin/zsh
cd -- "${0:A:h}" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv || exit 1
fi
# A copied pip script can still point at the previous Mac's absolute path.
print 'Shortsmaker 실행 환경을 확인하고 있습니다…'
if ! .venv/bin/python -c 'from importlib.util import find_spec; import sys; sys.exit(not all(find_spec(m) is not None for m in ("flask", "PIL", "boto3", "faster_whisper", "cv2", "jsonschema")))' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r requirements.txt || exit 1
fi
for tool in ffmpeg ffprobe; do
  if ! command -v "$tool" >/dev/null; then
    print "이 Mac에 $tool 설치가 필요합니다. Homebrew에서 brew install ffmpeg 후 다시 실행하세요."
    exit 1
  fi
done
if curl --connect-timeout 1 --max-time 2 -fsS http://127.0.0.1:5099/api/state >/dev/null 2>&1; then
  open http://127.0.0.1:5099
  exit 0
fi
(for attempt in {1..90}; do
  if curl --connect-timeout 1 --max-time 2 -fsS http://127.0.0.1:5099/api/state >/dev/null 2>&1; then
    open http://127.0.0.1:5099
    break
  fi
  sleep 1
done) &
print '앱 서버를 시작합니다. 잠시 뒤 브라우저가 열립니다.'
exec .venv/bin/python app.py
