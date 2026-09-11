#!/bin/zsh
cd -- "${0:A:h}"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv || exit 1
fi
# A copied pip script can still point at the previous Mac's absolute path.
if ! .venv/bin/python -c 'import flask, PIL, boto3, faster_whisper, cv2, jsonschema' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r requirements.txt || exit 1
fi
for tool in ffmpeg ffprobe; do
  if ! command -v "$tool" >/dev/null; then
    print "이 Mac에 $tool 설치가 필요합니다. Homebrew에서 brew install ffmpeg 후 다시 실행하세요."
    exit 1
  fi
done
if curl -fsS http://127.0.0.1:5099/api/state >/dev/null 2>&1; then
  open http://127.0.0.1:5099
  exit 0
fi
(for attempt in {1..90}; do
  if curl -fsS http://127.0.0.1:5099/api/state >/dev/null 2>&1; then
    open http://127.0.0.1:5099
    break
  fi
  sleep 1
done) &
exec .venv/bin/python app.py
