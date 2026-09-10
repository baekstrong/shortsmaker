#!/bin/zsh
cd -- "${0:A:h}"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv || exit 1
  .venv/bin/pip install -r requirements.txt || exit 1
fi
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
