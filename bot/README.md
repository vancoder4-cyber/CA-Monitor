# 交互式 Lark 机器人（CA 问答助手）

它是**按需查询入口**，和定时推送机器人不同：运行时只读取 GitHub Pages 的 `data.json`，不抓行情、不重算公司行动。这样同一份发布数据会被网页、定时推送和交互卡片共同使用。当前范围为现货 62、合约 39、覆盖资产 81、可监控证券 75；Bot 的实际范围以 Pages `coverage` 字段为准。

Bot 只接受当前 **schema v3** 且仍在 `valid_until_utc` 内的快照。网络失败、旧 schema、字段缺失、未来时间或过期数据都会返回红色「数据不可用」卡并停止输出「无风险 / 无需操作」等结论；帮助、需求提报和历史留痕仍可用。

## 能做什么

在群里 `@CA问答助手 + 关键词`（私聊可不 @）。当前 15 个指令的唯一来源是 `cards.py` 的 `COMMANDS`：

| 指令 | 用途 |
|---|---|
| 关于 / 帮助 / 最近更新 | 范围、规则、数据更新时间、版本记录 |
| 风险 / 今日 / 本周 | 风控清单（现货/合约动作分开）；本周按未来 7 个自然日内的除息/生效日统计正式事件并去重 |
| 新公告 / 临近催办 | 最近公司宣告；临近页只列距除息/生效 0–14 天的执行催办和数据核验。仅合约且价格影响 ≤3% 的事项仍正常报告，但不进重复催办 |
| 观察预测 | `观察 CODE YYYY-MM-DD [备注]`；单源按 30/14 天节奏推核验提醒，预测不执行，正式化/改期/失效也会推送 |
| 日历 / 覆盖 | 月历图和资产覆盖范围 |
| 查代码 | 单标的完整卡片，含宣告/登记/除息/派发、风险提示和核对链接 |
| 确认 / 留痕 | 人工确认异常及审计记录；按代码+类型+日期精确生效，拆/合股用 `确认 CODE 新股数:旧股数 日期` 输入完整比例；部分写入失败会明确报「未生效」 |
| 需求提报 | `需求 你的想法` 以匿名编号写入公开 GitHub；请勿填写敏感信息 |

分红卡片统一显示：**官方本次公告/IR/SEC** 链接 + **StockAnalysis 交叉核对（可能滞后）**。第三方不是正式化依据。若 Pages 已刷新而卡片仍显示旧样式，优先检查 Railway 是否已部署到当前 GitHub 提交。

合约动作由 Pages 统一下发：现金、送股、拆股、合股等的估算价格影响严格 `>3%` 才需操作，`≤3%` 固定显示「合约：本次无需操作」；缺金额/比例、币种/证券单位或参考价时显示待核实。Bot 不自行重算门槛。

日历图片由 `render.py` + Pillow 在 Bot 容器中生成，**不再截图网页**。

## Lark 应用设置

这是「自定义应用 App」，不同于定时推送用的 Webhook 自定义机器人；两者可以并存。

1. 在 [Lark 开发者后台](https://open.larksuite.com/app) 创建企业自建应用，取得 App ID / App Secret。
2. 启用机器人能力；订阅长连接事件 `im.message.receive_v1`。
3. 开通 `im:message`、`im:message:send_as_bot`、`im:resource`。
4. 发布应用并把机器人加入目标群。

## Railway 部署与变量

1. 选择 GitHub 仓库 `CA-Monitor`，将 **Root Directory** 设为 `bot`；该目录的 `Dockerfile` 会安装中文字体和 Bot 依赖。
2. 设置以下变量：

| 变量 | 是否必需 | 用途 |
|---|---:|---|
| `LARK_APP_ID` / `LARK_APP_SECRET` | 是 | Lark 长连接凭证 |
| `SITE_URL` | 是 | Pages 根地址，例如 `https://vancoder4-cyber.github.io/CA-Monitor/` |
| `GH_TOKEN` | 确认/观察/需求需要 | 仅此仓库 Contents 读写的细粒度 PAT |
| `GH_REPO` / `GH_BRANCH` | 可选 | 默认 `vancoder4-cyber/CA-Monitor` / `main` |
| `HEARTBEAT_URL` | 建议 | healthchecks 等死信监控地址；每 5 分钟 ping 一次 |

Railway 通常会自动提供 `RAILWAY_GIT_COMMIT_SHA`。`关于`卡片同时显示 Pages 数据 commit 与 Bot build commit；两者不同表示仍在发布过渡期，不能算验收完成。

3. 每次改动 `bot/` 后确认 Railway 自动部署完成；如果未绑定自动部署，手动点 **Deploy**。不要只看 GitHub Pages 绿灯：Pages 与 Railway 是两个独立发布面。

## 发布后验收

1. 先让 producer 的 GitHub Actions 跑绿，确认 Pages `/data.json` 已是 schema v3、`source_sha` 等于目标 main commit、`run_id` 指向本次任务且尚未超过有效时点。
2. 再确认 Railway 部署同一 main commit；若 Railway 抢先上线，Bot 会在 Pages 刷新前安全地 fail closed，不会用旧数据给结论。日志应出现「等待 @ 指令」且没有 import / 连接错误。
3. 群里先发 `关于`，确认「数据 commit」与「Bot build」一致；再测试 `帮助`、`查 AAPL`、`查 BBX`、`查 BRK-B`、`查 SKHY`、`临近催办`、`观察预测`、`最近更新`，`查 BBX` 应返回 canonical `BB` 卡片。
4. `最近更新` 必须完整显示 CHANGELOG 最新版本的全部条目；`查 AAPL` 应返回官方入口、StockAnalysis 交叉核对链接及产品动作结论。现金/送股/拆合股均要覆盖 `≤3%`「合约：本次无需操作」和 `>3%`「合约：需操作」边界。单源事件只能显示核验提醒，不能出现正式运营执行文案。
   `BBX` 会规范化为 `BB`，`BRKB` / `BRK.B` 会规范化为 `BRK-B`，`QNTX` 会规范化为 `QNT`。

## 本地调试

```bash
cd bot
pip install -r requirements.txt
export LARK_APP_ID=... LARK_APP_SECRET=... \
  SITE_URL=https://vancoder4-cyber.github.io/CA-Monitor/
python3 bot.py
```

在仓库根目录运行 `python3 tools/check_commands.py` 与 `python3 tools/check_surface_consistency.py`，可在发版前检查指令和统一链接契约。
