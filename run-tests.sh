#!/bin/bash
# signal-gate 离线测试统一入口：tests/ 无 __init__.py，整目录 discover 会失败，
# 按文件 pattern 逐个加载（与 README.md 的手动列表等价）。
# 零发现保护：tests/ 下没有 test_*.py 视为入口故障而非通过。
set -euo pipefail
cd "$(dirname "$0")"

shopt -s nullglob
patterns=(tests/test_*.py)
if (( ${#patterns[@]} == 0 )); then
  echo "ERROR: tests/ 下未发现任何 test_*.py，零发现视为失败" >&2
  exit 1
fi

rc=0
for f in "${patterns[@]}"; do
  pattern=$(basename "$f")
  echo "== ${pattern%.py} =="
  PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -p "$pattern" || rc=1
done
exit $rc
