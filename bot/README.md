# 交互式 Lark 机器人（CA 问答助手）

它是**按需查询入口**，和定时推送机器人不同：运行时只读取 GitHub Pages 的 `data.json`，不抓行情、不重算公司行动。这样同一份发布数据会被网页、定时推送和交互卡片共同使用。当前范围为现货 62、合约 22、覆盖资产 73、可监控证券 67；Bot 的实际范围以 Pages `coverage` 字段为准。

## 能做什么

在群里 `@CA问答助手 + 关键词`（私聊可不 @）。当前 15 个指令的唯一来源是 `cards.py` 的 `COMMANDS`：

| 指令 | 用途 |
|---|---|
| 关于 / 帮助 / 最近更新 | 范围、规则、数据更新时间、版本记录 |
| 风险 / 今日 / 本周 | 风控清单及窗口内关键日 |
| 新公告 / 临近催办 | 最近公司宣告、已正式确认的待执行事项 |
| 观察预测 | `观察 CODE YYYY-MM-DD [备注]`；预测不执行，后续正式化/改期/失效会推送 |
| 日历 / 覆盖 | 月历图和资产覆盖范围 |
| 查代码 | 单标的完整卡片，含宣告/登记/除息/派发、风险提示和核对链接 |
| 确认 / 留痕 | 人工确认异常及审计记录 |
| 需求提报 | `需求 你的想法` 写入需求队列 |

分红卡片统一显示：**官方本次公告/IR/SEC** 链接 + **StockAnalysis 交叉核对（可能滞后）**。第三方不是正式化依据。若 Pages 已刷新而卡片仍显示旧样式，优先检查 Railway 是否已部署到当前 GitHub 提交。

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

3. 每次改动 `bot/` 后确认 Railway 自动部署完成；如果未绑定自动部署，手动点 **Deploy**。不要只看 GitHub Pages 绿灯：Pages 与 Railway 是两个独立发布面。

## 发布后验收

1. GitHub Actions 先跑绿，确认 Pages 的 `/data.json` 已刷新。
2. Railway 日志应出现「等待 @ 指令」且没有 import / 连接错误。
3. 群里依次测试：`帮助`、`查 AAPL`、`查 BRK-B`、`查 SKHY`、`临近催办`、`观察预测`。
4. `查 AAPL` 应返回当前支持范围内的单标的卡片；如有分红事件，卡片应同时显示官方入口与 StockAnalysis 交叉核对链接，且正式事件不应带「预测观察·不执行」或运营催办与状态矛盾。
   `BRKB` / `BRK.B` 会规范化为 `BRK-B`，`QNTX` 会规范化为 `QNT`；`BBX` 未映射到 `BB`，待业务确认。

## 本地调试

```bash
cd bot
pip install -r requirements.txt
export LARK_APP_ID=... LARK_APP_SECRET=... \
  SITE_URL=https://vancoder4-cyber.github.io/CA-Monitor/
python3 bot.py
```

在仓库根目录运行 `python3 tools/check_commands.py` 与 `python3 tools/check_surface_consistency.py`，可在发版前检查指令和统一链接契约。
