#!/bin/bash
# signal-gate 离线测试统一入口：tests/ 无 __init__.py，整目录 discover 会失败，
# 按文件 pattern 逐个加载（与 README.md 的手动列表等价，不会漏新测试文件）。
set -euo pipefail
cd "$(dirname "$0")"
rc=0
for f in tests/test_*.py; do
  pattern=$(basename "$f")
  echo "== ${pattern%.py} =="
  PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -p "$pattern" || rc=1
done
exit $rc
