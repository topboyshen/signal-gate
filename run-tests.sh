#!/bin/bash
# signal-gate 离线测试统一入口（tests/ 无 __init__.py，整目录 discover 会失败，
# 按文件 pattern 逐个加载，与 README.md 的手动列表等价）。
# 零发现保护：tests/ 下没有 test_*.py 视为入口故障而非通过。
# 跨项目互斥: /var/lock/vps-tests/global.lock
# 单机资源约束: root → systemd-run scope；非 root → nice/ionice 软限额并明示
set -euo pipefail
cd "$(dirname "$0")"

GLOBAL_LOCK="/var/lock/vps-tests/global.lock"
mkdir -p .test-runs
TS=$(date -u +%Y%m%dT%H%M%SZ)
LOG_FILE=".test-runs/run-$TS.log"
touch "$LOG_FILE"
chmod 0660 "$LOG_FILE"

exec 200>"$GLOBAL_LOCK"
if ! flock -n 200; then
  echo "SKIP: 另一测试进程持有全局锁（$GLOBAL_LOCK），防重生效" >&2
  exit 75
fi

if [[ "$(id -u)" == "0" ]]; then
  if command -v systemd-run >/dev/null 2>&1; then
    PREFIX=(systemd-run --scope --quiet --collect
      -p CPUQuota=70% -p MemoryMax=800M -p MemorySwapMax=0
      nice -n 19 ionice -c3)
  else
    echo "FAIL: root 模式下 systemd-run 不可用，拒绝静默降级" >&2
    exit 9
  fi
else
  if ! command -v nice >/dev/null 2>&1; then
    echo "FAIL: nice 不可用，拒绝执行" >&2
    exit 9
  fi
  PREFIX=(nice -n 19)
  if command -v ionice >/dev/null 2>&1; then
    PREFIX+=(ionice -c3)
  else
    echo "WARN: ionice 不可用" >&2
  fi
  echo "WARN: 非 root 路径无 CPUQuota/MemoryMax 硬约束" >&2
fi

shopt -s nullglob
patterns=(tests/test_*.py)
if (( ${#patterns[@]} == 0 )); then
  echo "ERROR: tests/ 下未发现任何 test_*.py，零发现视为失败" >&2
  exit 1
fi

{
  echo "=== signal-gate test run $TS ==="
  echo "user: $(id -un)($(id -u)) host: $(hostname)"
  echo "load: $(cat /proc/loadavg)"
  echo "prefix: ${PREFIX[*]}"
  echo
} >> "$LOG_FILE" 2>&1

rc=0
for f in "${patterns[@]}"; do
  pattern=$(basename "$f")
  echo "== ${pattern%.py} ==" >> "$LOG_FILE"
  "${PREFIX[@]}" env PYTHONDONTWRITEBYTECODE=1 python3 -B \
    -m unittest discover -s tests -p "$pattern" \
    >> "$LOG_FILE" 2>&1 || rc=1
done

ls -1t .test-runs/run-*.log 2>/dev/null | tail -n +31 | xargs -r rm -f -- 2>/dev/null || true

echo "" >> "$LOG_FILE"
echo "=== finished exit=$rc at $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" >> "$LOG_FILE"
if [[ "$(id -u)" == "0" ]]; then
  echo "resource-limits: CPUQuota=70% MemoryMax=800M (via systemd-run scope)" >> "$LOG_FILE"
fi
echo "log: $LOG_FILE"
tail -25 "$LOG_FILE"
exit $rc
