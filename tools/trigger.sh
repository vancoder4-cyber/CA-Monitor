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
cd "$(dirname "$0")/.."
REPO="vancoder4-cyber/CA-Monitor"
WF="monitor.yml"
SITE="https://vancoder4-cyber.github.io/CA-Monitor"

command -v gh >/dev/null 2>&1 || {
  echo "❌ 没装 gh。先跑:brew install gh && gh auth login"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "❌ gh 未登录。先跑:gh auth login"; exit 1; }

echo "▶ 触发 $WF (main) …"
EXPECTED_SHA=$(gh api "repos/$REPO/commits/main" --jq .sha)
EXPECTED_CHANGELOG_HEAD=$(awk '/^## /{sub(/^## /, ""); print; exit}' CHANGELOG.md)
STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
gh workflow run "$WF" --repo "$REPO" --ref main

# 不能直接取“最新 run”：定时任务或别人手动触发可能并发。按 event、main HEAD
# 和触发时间锁定本次 workflow_dispatch。
RUN_ID=""
for _ in {1..30}; do
  RUN_ID=$(gh run list --repo "$REPO" --workflow "$WF" --event workflow_dispatch \
    --branch main --limit 20 --json databaseId,headSha,createdAt \
    --jq ".[] | select(.headSha == \"$EXPECTED_SHA\" and .createdAt >= \"$STARTED_AT\") | .databaseId" \
    | head -n 1)
  [[ -n "$RUN_ID" ]] && break
  sleep 2
done
[[ -n "$RUN_ID" ]] || { echo "❌ 60 秒内未找到本次 workflow_dispatch run"; exit 1; }
echo "▶ run id: $RUN_ID"
echo "   https://github.com/$REPO/actions/runs/$RUN_ID"

if [[ "${1:-}" == "-n" ]]; then
  echo "✅ 已触发(不等待)。"; exit 0
fi

echo "▶ 等待跑完 …"
gh run watch "$RUN_ID" --repo "$REPO" --exit-status || {
  echo "❌ Action 失败,看日志:gh run view $RUN_ID --repo $REPO --log-failed"; exit 1; }

echo "▶ 核验 Pages 数据版本、投递状态和更新日志 …"
VERIFIED=""
for attempt in {1..30}; do
  if RESULT=$(curl -fsSL "$SITE/data.json?run=$RUN_ID&attempt=$attempt" | \
      EXPECTED_SHA="$EXPECTED_SHA" EXPECTED_RUN_ID="$RUN_ID" \
      EXPECTED_CHANGELOG_HEAD="$EXPECTED_CHANGELOG_HEAD" python3 -c '
import datetime as dt, json, os, sys
d=json.load(sys.stdin)
checks={
  "schema_version": d.get("schema_version") == 3,
  "source_sha": d.get("source_sha") == os.environ["EXPECTED_SHA"],
  "run_id": str(d.get("run_id")) == os.environ["EXPECTED_RUN_ID"],
  "delivery_status": d.get("delivery_status") in ("sent", "legal_skip"),
  "changelog": bool(d.get("changelog")) and d["changelog"][0].get("head") == os.environ["EXPECTED_CHANGELOG_HEAD"],
}
times={}
for field in ("generated_at_utc", "valid_until_utc"):
  try: times[field]=dt.datetime.fromisoformat(str(d[field]).replace("Z", "+00:00"))
  except Exception: checks[field]=False
if len(times) == 2:
  checks["validity_window"] = times["generated_at_utc"] < times["valid_until_utc"]
  checks["not_expired"] = dt.datetime.now(dt.timezone.utc) <= times["valid_until_utc"]
if not all(checks.values()):
  print("waiting:"+",".join(k for k,v in checks.items() if not v), file=sys.stderr)
  raise SystemExit(1)
print("{} · delivery={}".format(d.get("generated", "?"), d.get("delivery_status", "?")))
'); then
    VERIFIED="$RESULT"
    break
  fi
  sleep 5
done
[[ -n "$VERIFIED" ]] || { echo "❌ Action 已成功，但 Pages 150 秒内仍未发布本次可验证快照"; exit 1; }
echo "✅ 完成。网页数据生成于:$VERIFIED"
echo "   commit:$EXPECTED_SHA · run:$RUN_ID · schema:v3"
echo "   $SITE/"
