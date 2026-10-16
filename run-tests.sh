#!/bin/bash
# signal-gate 测试执行体 (v4, 含 --reviewed 模式)
# 由 /usr/local/bin/run-tests-launcher 启动 (root + 全局锁 + systemd-run scope).
# 直接执行仅在不与锁冲突的两种情况下允许:
#   (1) RUNNER_HOLDLOCK=1  (父进程是受控启动层并已持锁)
#   (2) 该进程自身能拿到全局锁 (root 守护场景; 非 root 若锁存在但无写权限会被拒)
# 否则明确拒绝 (exit 9), 杜绝"裸跑无硬限额"路径.
set -euo pipefail
cd "$(dirname "$0")"

GLOBAL_LOCK="/var/lock/vps-tests/global.lock"
mkdir -p .test-runs
TS=$(date -u +%Y%m%dT%H%M%SZ)
LOG_FILE=".test-runs/run-$TS.log"
touch "$LOG_FILE"; chmod 0660 "$LOG_FILE"

MODE="full"
args=()
for a in "$@"; do
  case "$a" in
    --reviewed) MODE="reviewed";;
    *) args+=("$a");;
  esac
done
if (( ${#args[@]} > 0 )); then set -- "${args[@]}"; else set --; fi

if [[ "${RUNNER_HOLDLOCK:-}" == "1" ]]; then
  HELD_BY_PARENT=1
elif [[ "$(id -u)" == "0" ]]; then
  # root 单跑 (preflight 经 wrapper 已是这层, 但保留 root 兜底兼容)
  exec 200>"$GLOBAL_LOCK"
  if ! flock -n 200; then
    echo "SKIP: 另一测试/preflight 进程持有全局锁 ($GLOBAL_LOCK), 防重生效" >&2
    exit 75
  fi
  HELD_BY_PARENT=0
else
  echo "FAIL: 非 root 路径必须经 /usr/local/bin/run-tests-launcher 启动 (RUNNER_HOLDLOCK=1)" >&2
  echo "      直接执行无法建立 CPUQuota/MemoryMax 硬限额, 拒绝运行" >&2
  exit 9
fi

if [[ "$(id -u)" == "0" && "$HELD_BY_PARENT" == "0" ]]; then
  if command -v systemd-run >/dev/null 2>&1; then
    PREFIX=(systemd-run --scope --quiet --collect
      -p CPUQuota=70% -p MemoryMax=800M -p MemorySwapMax=0
      nice -n 19 ionice -c3)
  else
    echo "FAIL: root 裸跑路径下 systemd-run 不可用" >&2
    exit 9
  fi
elif [[ "$(id -u)" == "0" ]]; then
  PREFIX=(nice -n 19)
else
  PREFIX=(nice -n 19)
fi

{
  echo "=== signal-gate test run $TS mode=$MODE ==="
  echo "user: $(id -un)($(id -u)) host: $(hostname) held_by_parent=$HELD_BY_PARENT"
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
  for suite in "${REVIEWED_SUITES[@]}"; do
    test -f "tests/$suite" || { echo "missing suite: $suite" >>"$LOG_FILE"; rc=1; break; }
    "${PREFIX[@]}" env PYTHONDONTWRITEBYTECODE=1       timeout 60 /usr/bin/python3 -B -m unittest discover -s tests -p "$suite" -v       >> "$LOG_FILE" 2>&1 || { rc=1; break; }
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
    "${PREFIX[@]}" env PYTHONDONTWRITEBYTECODE=1 python3 -B       -m unittest discover -s tests -p "$pattern"       >> "$LOG_FILE" 2>&1 || rc=1
  done
fi

ls -1t .test-runs/run-*.log 2>/dev/null | tail -n +31 | xargs -r rm -f -- 2>/dev/null || true
echo "" >> "$LOG_FILE"
echo "=== finished exit=$rc at $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" >> "$LOG_FILE"
if [[ "$HELD_BY_PARENT" == "1" ]]; then
  echo "resource-limits: enforced by run-tests-launcher (CPUQuota=70% MemoryMax=800M; lock held by parent)" >> "$LOG_FILE"
elif [[ "$(id -u)" == "0" ]]; then
  echo "resource-limits: CPUQuota=70% MemoryMax=800M (root systemd-run scope)" >> "$LOG_FILE"
fi
echo "log: $LOG_FILE"
tail -25 "$LOG_FILE"
exit "${rc:-0}"
