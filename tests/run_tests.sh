#!/usr/bin/env bash
# 本地自检脚本：语法、配置、WebUI 脚本与测试套件。
# 不需要安装 AstrBot，也不需要网络。
#
#   bash tests/run_tests.sh
#
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> 1/4 检查 main.py 语法"
python3 -m py_compile main.py
echo "    OK"

echo "==> 2/4 校验 _conf_schema.json"
python3 - <<'PY'
import json

with open("_conf_schema.json", encoding="utf-8") as f:
    data = json.load(f)
assert "rarity_weights" in data and "pity_config" in data
print("    OK，字段:", ", ".join(data))
PY

echo "==> 3/4 检查 WebUI Page 脚本"
if command -v node >/dev/null 2>&1; then
    node --check pages/admin/app.js
    echo "    OK"
else
    echo "    跳过（未安装 node）"
fi

echo "==> 4/4 运行测试套件"
python3 -m unittest discover -s tests

echo
echo "全部检查通过 ✅"
