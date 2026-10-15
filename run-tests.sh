#!/bin/bash
# signal-gate 离线测试统一入口：tests/ 无 __init__.py，整目录 discover 会失败，
# 按文件 pattern 逐个加载（与 README.md 的手动列表等价）。
# 零发现保护：tests/ 下没有 test_*.py 视为入口故障而非通过。
# 防重: flock 非阻塞锁
# 资源限制: nice + ionice + 可选 systemd-run scope
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p .test-runs
TS=$(date -u +%Y%m%dT%H%M%SZ)
LOG_FILE=".test-runs/run-$TS.log"
LOCK_FILE=".test-runs/.lock"

exec 200>"$LOCK_FILE"
if ! flock -n 200; then
  echo "SKIP: 另一个测试进程持有锁" >&2
  exit 75
fi

{
  echo "=== signal-gate test run $TS ==="
  echo "load: $(cat /proc/loadavg)"
  echo
} >> "$LOG_FILE" 2>&1

shopt -s nullglob
patterns=(tests/test_*.py)
if (( ${#patterns[@]} == 0 )); then
  echo "ERROR: tests/ 下未发现任何 test_*.py，零发现视为失败" >&2
  exit 1
fi

PREFIX=(nice -n 19 ionice -c3 -n7)
if command -v systemd-run >/dev/null 2>&1 && [[ "$(id -u)" == "0" ]]; then
  PREFIX=(systemd-run --scope --quiet --collect \
    -p CPUQuota=70% -p MemoryMax=800M -p MemorySwapMax=0 \
    "${PREFIX[@]}")
fi

rc=0
for f in "${patterns[@]}"; do
  pattern=$(basename "$f")
  echo "== ${pattern%.py} ==" >> "$LOG_FILE"
  "${PREFIX[@]}" env PYTHONDONTWRITEBYTECODE=1 python3 -B \
    -m unittest discover -s tests -p "$pattern" \
    >> "$LOG_FILE" 2>&1 || rc=1
done

# rotate logs: keep last 30
ls -1t .test-runs/run-*.log 2>/dev/null | tail -n +31 | xargs -r rm -f -- 2>/dev/null || true

echo "" >> "$LOG_FILE"
echo "=== finished exit=$rc at $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" >> "$LOG_FILE"
echo "log: $LOG_FILE"
tail -25 "$LOG_FILE"
exit $rc
