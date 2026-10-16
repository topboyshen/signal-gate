#!/bin/bash
# signal-gate 测试执行体 (v5, 含 --reviewed 模式)
# 由受控启动层调用. 关键变更 (v4 → v5):
#   - RUNNER_HOLDLOCK=1 不再作为保护生效证据, 仅作"父进程意图"提示
#   - 启动时实际验证:
#       A) 本进程或祖先持有全局锁 (/proc/locks 检查)
#       B) 当前 cgroup 有真实 cpu.max / memory.max 硬限额 (读 /sys/fs/cgroup/<cg>/)
#     任一验证失败 → exit 9, 不进入测试执行
#   - 业务用户锁外裸跑拒 (exit 2); root 锁外裸跑拒 (exit 9)

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

# --- 保护层实证 (v5) ---
PROTECT_LIB="/usr/local/lib/vpstest/protection-lib.sh"
[[ -r "$PROTECT_LIB" ]] || { echo "FAIL: $PROTECT_LIB 不可读 (protection 库未部署)" >&2; exit 9; }
# 保护库必须是 root:root 0644 — 被替换则拒绝
_owner=$(stat -Lc "%U:%G" "$PROTECT_LIB")
_mode=$(stat -Lc "%a" "$PROTECT_LIB")
if [[ "$_owner" != "root:root" || "$_mode" != "644" ]]; then
  echo "FAIL: $PROTECT_LIB 权限/owner 不符 (期望 root:root 0644, 实际 $_owner $_mode)" >&2
  exit 9
fi
unset _owner _mode
# shellcheck disable=SC1090
source "$PROTECT_LIB"

if ! verify_lock_held_by_self_or_ancestor "$GLOBAL_LOCK"; then
  echo "FAIL: 本进程或祖先未持有全局锁 $GLOBAL_LOCK (/proc/locks 验证)" >&2
  echo "      请经 /usr/local/bin/run-tests-launcher 受控启动, 或 vps-preflight-safe 内运行" >&2
  exit 9
fi

# cgroup 期望: CPU ≤ 70%, memory ≤ 800MiB (允许更严, 不允许更松)
if ! verify_cgroup_limits_or_die 70 $((800*1024*1024)); then
  echo "FAIL: 当前 cgroup 缺少硬限额或超过预算" >&2
  echo "      请经 /usr/local/bin/run-tests-launcher 受控启动 (建立 systemd-run scope)" >&2
  exit 9
fi

# 通过实证后:
PREFIX=(nice -n 19)
LOCK_EVIDENCE="flock verified via /proc/locks (holder pid in self/ancestor lineage)"
CGROUP_EVIDENCE="$(cat /sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup)/cpu.max 2>/dev/null)/$(cat /sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup)/memory.max 2>/dev/null)"

{
  echo "=== signal-gate test run $TS mode=$MODE ==="
  echo "user: $(id -un)($(id -u)) host: $(hostname)"
  echo "load: $(cat /proc/loadavg)"
  echo "lock-evidence: $LOCK_EVIDENCE"
  echo "cgroup-evidence: $CGROUP_EVIDENCE"
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
echo "=== finished exit=$rc at $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" >> "$LOG_FILE"
echo "lock-evidence: $LOCK_EVIDENCE" >> "$LOG_FILE"
echo "cgroup-evidence: $CGROUP_EVIDENCE" >> "$LOG_FILE"
echo "log: $LOG_FILE"
tail -25 "$LOG_FILE"
exit "${rc:-0}"
