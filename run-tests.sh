#!/bin/bash
# signal-gate 离线测试统一入口
#   默认（手动）             : tests/test_*.py 全量（含零发现保护）
#   --reviewed              : 与 vps-preflight 相同的 10 套件 allowlist
# 跨项目互斥: /var/lock/vps-tests/global.lock（flock 非阻塞）
# 资源约束: root → systemd-run scope；非 root → nice/ionice 软限额
set -euo pipefail
cd "$(dirname "$0")"

GLOBAL_LOCK="/var/lock/vps-tests/global.lock"
mkdir -p .test-runs
TS=$(date -u +%Y%m%dT%H%M%SZ)
LOG_FILE=".test-runs/run-$TS.log"
touch "$LOG_FILE"; chmod 0660 "$LOG_FILE"

# --- 模式选择 ---
MODE="full"
args=()
for a in "$@"; do
  case "$a" in
    --reviewed) MODE="reviewed";;
    *) args+=("$a");;
  esac
done
if (($# > 0)); then set -- "${args[@]+"${args[@]}"}"; else set --; fi

# --- 全局互斥 ---
exec 200>"$GLOBAL_LOCK"
if ! flock -n 200; then
  echo "SKIP: 另一测试进程持有全局锁（$GLOBAL_LOCK），防重生效" >&2
  exit 75
fi

# --- 资源控制 ---
if [[ "$(id -u)" == "0" ]]; then
  if command -v systemd-run >/dev/null 2>&1; then
    PREFIX=(systemd-run --scope --quiet --collect
      -p CPUQuota=70% -p MemoryMax=800M -p MemorySwapMax=0
      nice -n 19 ionice -c3 -n7)
  else
    echo "FAIL: root 模式下 systemd-run 不可用" >&2
    exit 9
  fi
else
  PREFIX=(nice -n 19)
  command -v ionice >/dev/null 2>&1 && PREFIX+=(ionice -c3)
  echo "WARN: 非 root 路径仅 nice/ionice 软限额" >&2
fi

{
  echo "=== signal-gate test run $TS mode=$MODE ==="
  echo "user: $(id -un)($(id -u)) host: $(hostname)"
  echo "load: $(cat /proc/loadavg)"
  echo "prefix: ${PREFIX[*]}"
  echo
} >> "$LOG_FILE" 2>&1

REVIEWED_SUITES=(
  test_offline.py test_digest_content.py test_digest_delivery.py
  test_sealed_research.py test_news_history.py
  test_research_receipts.py test_research_receiver.py
  test_input_status.py test_research_contract.py test_review_outbox.py
)

rc=0
if [[ "$MODE" == "reviewed" ]]; then
  # 与 vps-preflight 原版完全一致的调用模式（保持 contract test 兼容），
  # 但加上 PREFIX(资源控制)，且每套件 fail 立即 exit 1（与原 for ... || exit 1 一致）。
  for suite in "${REVIEWED_SUITES[@]}"; do
    test -f "tests/$suite" || { echo "missing suite: $suite" >>"$LOG_FILE"; rc=1; break; }
    "${PREFIX[@]}" env PYTHONDONTWRITEBYTECODE=1 \
      timeout 60 /usr/bin/python3 -B -m unittest discover -s tests -p "$suite" -v \
      >> "$LOG_FILE" 2>&1 || { rc=1; break; }
  done
else
  shopt -s nullglob
  patterns=(tests/test_*.py)
  if (( ${#patterns[@]} == 0 )); then
    echo "ERROR: tests/ 下未发现任何 test_*.py，零发现视为失败" >&2
    exit 1
  fi
  for f in "${patterns[@]}"; do
    pattern=$(basename "$f")
    echo "== ${pattern%.py} ==" >> "$LOG_FILE"
    "${PREFIX[@]}" env PYTHONDONTWRITEBYTECODE=1 python3 -B \
      -m unittest discover -s tests -p "$pattern" \
      >> "$LOG_FILE" 2>&1 || rc=1
  done
fi

ls -1t .test-runs/run-*.log 2>/dev/null | tail -n +31 | xargs -r rm -f -- 2>/dev/null || true
echo "" >> "$LOG_FILE"
echo "=== finished exit=$rc mode=$MODE at $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" >> "$LOG_FILE"
[[ "$(id -u)" == "0" ]] && echo "resource-limits: CPUQuota=70% MemoryMax=800M (scope)" >> "$LOG_FILE"
echo "log: $LOG_FILE"
tail -25 "$LOG_FILE"
exit $rc
