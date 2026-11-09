#!/usr/bin/env bash
# 用法：work/ops/with_library_lock.sh <命令...>
# 拿 work/.library.lock（flock，排他）后执行命令；别人拿着锁就排队等（默认最多 1800 秒，环境变量 LOCK_WAIT 可改）。
# 规矩：凡是写 library.json、跑 build.py、git push deploy/ 的，都必须包在这把锁里。
#   例：work/ops/with_library_lock.sh python3 work/apply_ratings.py --publish
#       work/ops/with_library_lock.sh bash -c 'python3 work/ingest_2026_11_04.py && python3 build.py && cd deploy && git add -A index.html img && git commit -m "daily ..." && git push origin main'
set -euo pipefail
D="$(cd "$(dirname "$0")/.." && pwd)"     # work/
LOCK="$D/.library.lock"
[ $# -ge 1 ] || { echo "用法：$0 <命令...>" >&2; exit 2; }
exec 9>>"$LOCK"
if ! flock -w "${LOCK_WAIT:-1800}" 9; then
  echo "等 work/.library.lock 超时（${LOCK_WAIT:-1800} 秒），没执行：$*" >&2; exit 75
fi
echo "with_library_lock pid=$$ since=$(date -Iseconds) cmd=$*" > "$LOCK.owner" 2>/dev/null || true
"$@"
