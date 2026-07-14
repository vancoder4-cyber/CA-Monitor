#!/usr/bin/env bash
# 触发一次 CA Monitor 的 GitHub Action,等它跑完,并核验网页是否刷新。
#
# 用法:
#   ./tools/trigger.sh          # 触发 + 等结果 + 核验
#   ./tools/trigger.sh -n       # 只触发,不等
#
# 依赖:GitHub CLI(gh)。一次性安装 + 登录:
#   brew install gh && gh auth login
# (workflow_dispatch 需要 Actions:write 权限,GH_TOKEN 那个细粒度 PAT 不够,所以用 gh 的登录态)

set -euo pipefail
REPO="vancoder4-cyber/CA-Monitor"
WF="monitor.yml"
SITE="https://vancoder4-cyber.github.io/CA-Monitor"

command -v gh >/dev/null 2>&1 || {
  echo "❌ 没装 gh。先跑:brew install gh && gh auth login"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "❌ gh 未登录。先跑:gh auth login"; exit 1; }

echo "▶ 触发 $WF (main) …"
gh workflow run "$WF" --repo "$REPO" --ref main
sleep 6

RUN_ID=$(gh run list --repo "$REPO" --workflow "$WF" --limit 1 --json databaseId -q '.[0].databaseId')
echo "▶ run id: $RUN_ID"
echo "   https://github.com/$REPO/actions/runs/$RUN_ID"

if [[ "${1:-}" == "-n" ]]; then
  echo "✅ 已触发(不等待)。"; exit 0
fi

echo "▶ 等待跑完 …"
gh run watch "$RUN_ID" --repo "$REPO" --exit-status || {
  echo "❌ Action 失败,看日志:gh run view $RUN_ID --repo $REPO --log-failed"; exit 1; }

echo "▶ 核验网页刷新 …"
sleep 8
GEN=$(curl -fsSL "$SITE/data.json" | python3 -c "import sys,json;print(json.load(sys.stdin).get('generated','?'))")
echo "✅ 完成。网页数据生成于:$GEN"
echo "   $SITE/"
