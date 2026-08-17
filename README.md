# 公司行动预警面板(多源交叉核对)

盯住一篮子标的(**现货 62 + 合约 22，共 73 个覆盖资产，其中 67 个可监控证券**)的公司行动(分红 / 拆股·合股 / 并购 / 分拆 / 退市),**8 源并行抓取 → 归一化 → 零容忍交叉核对 → 预测观察 / 报警 / 人工确认**。逻辑接近机构的 golden-copy 做法。

三个核心设计:
1. **零容忍**:同一事件多源比对,任一字段不一致或某源缺失就报警,**不做任何口径豁免**。
2. **金额门禁**:只有「多源交叉验证过且无冲突」的金额才是可执行确定值;有冲突或只有单源绝不作为确定值展示。未见宣告日的单源值仅以「预测」保留给跟踪，防止运营照着未核实的值执行。
3. **预测观察 + 人工介入闭环**:未宣告的单源预估只观察、不催办，自动跟踪升级/改期/失效；字段冲突和数据空缺则每次扫描重报、显示「已挂 N 天」,超期自动 @ 负责人。

产出:一屏看全的 HTML 面板(日历 + 预警 + 更新日志)+ Lark 推送 + @机器人问答。

### 分红的官方化与核对链接（所有展示面一致）

`run.py` 为每条分红预先生成同一份 `references`：**已逐项核验的官方本次公告/IR → SEC 本次宣告 8-K → 公司 IR 分红页 → SEC 公司备案**，并始终附 **StockAnalysis（交叉核对，可能滞后）**。网页、定时推送、单标的查询、临近催办、今日/本周、日历和新公告均消费这同一份数据；**Nasdaq 仅为采集源，绝不再作为唯一核对链接**。

已经确认公司正式宣告、但采集源尚未补齐字段时，在 `refs.json` 的 `official_event_overrides` 登记该事件的官方 URL、核验日期和已确认字段。它会参与零容忍核对；与采集源不一致仍会报警，不会静默覆盖。已退出现货范围的 V 2026-08-11 分红保留为本地历史回归 fixture，不会再被发布为当前预警或覆盖标的。

## 数据源(8 源,3 类角色)

| 源 | 角色 | Key | 覆盖 |
|---|---|---|---|
| **yfinance** | 分红/拆股(历史) | 免 | 尽力覆盖 |
| **Nasdaq** | 分红(按票)+ 拆股(市场日历) | 免 | 尽力覆盖；仅采集，不作唯一核对页 |
| **Tiingo** | 分红/拆股交叉源 | 免费 token | 尽力覆盖 |
| **Alpaca** | 分红/拆股 + **并购/分拆/退市结构化** | 免费 key(ID+Secret) | 尽力覆盖；单源无宣告日只作预测 |
| **FMP** | 分红/拆股 | 免费 key | 部分票(免费版 402 限额) |
| **Alpha Vantage** | 分红/拆股 | 免费 key | 尽力(免费 25 次/天,易限流) |
| **SEC EDGAR** | 并购/退市 filing(权威) | 免 | 覆盖可监控的美股/ETF，8-K/S-4/25-NSE 等 |
| **FINX(TRKD-HS)** | 分红/拆股/并购(JWT) | `FINX_USER`+`FINX_PASS` | 已上线;供方接口仍在调整期。**未配置凭证则静默跳过,不影响其它源** |

> 关键设计:源被限流/付费墙时标「**不可用**」而非「空缺」,绝不把"没查到"误判成"源说没有"。
>
> **FINX 备注**:认证 `POST /auth/token` 换 JWT,其余请求带 `x-auth-token` header;用 RIC(如 `TSLA.O`)寻址,映射见 `config.FINX_RIC`(默认 `.O`,接口稳定后按实际可调)。凭证只走环境变量,代码不留明文。

## 快速开始

```bash
pip install -r requirements.txt          # 依赖(yfinance, requests)
cp .env.example .env                      # 填入你的 key(见下)
python run.py                             # 全量:抓取 + 出面板(适合定时任务)
```

其它用法:

```bash
python run.py fetch                       # 仅抓取全量(并发),缓存到 data/cache/
python run.py fetch AAPL NVDA             # 仅抓指定票(调试)
python run.py build                       # 用缓存合并 → dashboard.html + 预警 digest
```

产出:
- `dashboard.html` —— **单页站点,顶部标签切换三个视图**:
  - 📅 **公司行动日历**:月历视图,分红/拆股/并购按日期铺格;每个事件标除息(主块带金额)/ 登记 / 派发三个关键日,悬停看完整日期;冲突红框、单源黄框
  - 🔔 **预警面板**:未来事件时间线 + 报警区(新发现/临近/冲突/空缺)+ 源健康矩阵
- `data/latest_digest.txt` —— 定时推送用的纯文本预警清单
- `data/state.json` —— 记录已见事件(新发现判定)与已触发预警轮次(去重)
- `data/forecast_watch.json` —— 人工标记的预测观察项；未获证实前不进入执行催办

## 报警逻辑

- **新发现**:本次出现、上次没见过的事件(近 30 天内)
- **临近预警(运营催办)**:进入距除息/生效日 **30 天**窗口知会一次；15–29 天安静；**≤14 天每天**催办一次（一天三次扫描也只推一次），7/3/1 天仅升级催办文案
- **待执行**:已公告未发生的事件，持续展示 + 倒计时；未解决冲突绝不进入催办
- **预测观察**:单源且未见宣告日的预估，**不进入执行催办**；等待公司宣告或第二个独立源，改期/升级/失效会主动推送
- **字段冲突(零容忍)**:≥2 源对同一事件的 除权日/登记日/派发日/金额/拆股比例 有任何差异
- **数据空缺**:近 200 天内,某个"在覆盖该票"的源缺了别的源有的事件

只对「近 200 天 + 未来」的事件做冲突/空缺判定,避免老历史的覆盖深度差异造成噪音。

每条事件展示完整关键日链:**宣告 · 登记 · 除息/生效 · 派发**。

## 取值规则与金额门禁(核心)

**取值**:金额/比例一律走 `reconcile.pick_value()` —— **多数票 + 源优先级**(`config.SRC_PRIORITY`)。
**绝不能用「第一个源赢」**,因为源的顺序不代表谁对。各源口径差异(踩过的坑):

| 源 | 坑 | 实例 |
|---|---|---|
| yfinance | 按**拆股回溯调整**历史分红 | 历史拆股案例：KLAC 10:1 后把 `2.3` 报成 `0.23` |
| yfinance | **四舍五入**到 3 位 | 历史金额案例：WMT `0.2475` → `0.248` |
| Alpaca | 对 **ADR 报扣预扣税后的净额** | 历史 ADR 案例：ASML = gross×0.85(荷兰15%);TSM ×0.79(台湾21%) |

**🚦 金额门禁**:只有「**多源交叉验证过 且 无未确认冲突**」的金额才显示确定值。其它值均不可执行:

- 各源不一致 → `⚠️各源不一致(a / b)· 待人工确认,勿据此执行`
- 只有 1 个采集源报 → `⚠️单源未交叉验证(x)· 待人工确认,勿据此执行`；已逐项核验的公司官方公告/IR 例外，但仍保留全部来源与核对链接
- 无宣告日 + 单源 → `🔎预测观察(可能是预估,公司尚未正式公告；不执行)`；为便于追踪变动会保留为「预测值」，但绝不是确认金额

目的:**防止运营照着一个没人核过的数字去执行**。

## 人工介入闭环(零容忍·不豁免)

异常 = 字段冲突 / 数据空缺。未见宣告日的单源预估走「预测观察」：不进入催办、不要求人工确认，持续等公司宣告或第二个独立源；预计日期/金额变化或预计日已过仍未证实，会主动推送。

1. **不做口径豁免** —— ADR 扣税、拆股回溯、四舍五入造成的差异照报。
2. **每次扫描都重报**,不确认就一直挂;推送与官网显示「**已挂 N 天**」。
3. 超过 `REVIEW_ESCALATE_DAYS`(默认 3 天)没人确认 → 推送顶部 **@ 负责人**升级。
4. **消解方式**:群里发 `确认 代码 [正确值]`(如 `确认 AAPL 0.26`)
   → 门禁解除、停报警、按确认值显示,并留痕(谁确认、何时、以什么值为准)。
   确认对**所有事件**生效(不只是冲突,单源事件也能人工放行)。

## 配置(`config.py`)

- `SPOT_TICKERS` / `CONTRACT_TICKERS` —— 现货 **62 支** / 合约 22(含 ETF 与商品/海外)，合计 73 个覆盖资产
- `TICKERS` —— 实际监控标的 **67 支**(现货个股 + QQQ/EWY/DRAM ETF + SKHY；商品列入覆盖但不监控)
  - 代码格式坑:Berkshire B 类抓取 canonical ticker 必须写 **`BRK-B`**；Bot 同时接受 RFQ 输入 `BRKB` / `BRK.B`。`QNTX` 在 Bot 中会规范化为 `QNT`。
  - `BBX` 不会被静默映射为 `BB`（BlackBerry）：两者是否是同一 RFQ 标的须由业务确认后再配置。
  - `BASELINE_NEW_TICKERS` —— 新标的首次纳入时,历史事件是否静默建基线。`False`(默认)= 照常推「新发现」(历史上一次大批量上新会刷屏但能看全);`True` = 记为已见但不推(不刷屏)。此前一次大批量上新实测:False→72 条,True→0 条
- `ALERT_HEADSUP_DAY` / `ALERT_DAILY_WITHIN` —— 30 天一次知会 / 14 天内每日催办；`ALERT_ROUNDS` 仅保留给兼容旧调用
- `GROUP_WINDOW_DAYS` —— 跨源归组时间窗(默认 5 天)
- API key —— **全部从 `.env` / 环境变量读取,代码里不留明文**:
  `FMP` / `ALPHAVANTAGE` / `TIINGO` / `ALPACA_KEY_ID` / `ALPACA_SECRET` / `SEC_UA` / `FINX_USER` / `FINX_PASS`(可选,FINX 第 8 源;`FINX_BASE` 可改 UAT)
- `GH_TOKEN` —— 细粒度 PAT(Contents 读写),供「确认 / 预测观察 / 需求提报」写回仓库(配在 Railway)

**一键触发 Action**:`./tools/trigger.sh`(触发 + 等跑完 + 核验网页刷新;需 `brew install gh && gh auth login`)。

**可维护文件(改完提交即可)**:`refs.json`(官方 IR / 已核验事件 / 催办 @ 名单)、`CHANGELOG.md`(每次必记一条)、`UPDATE_CHECKLIST.md`(收尾检查清单)、`TODO.md`(内部技术待办/后续跟进)。`requests.md` 会在首次「需求」提报时自动创建。

## 密钥与安全

- `.env` 含真实密钥,**已在 `.gitignore`,绝不要提交到 GitHub**。
- 部署到生产时,优先用平台的 Secrets / 环境变量注入,而不是把 `.env` 打进镜像。
- 免费 key 申请:Alpha Vantage `alphavantage.co/support/#api-key`、FMP `site.financialmodelingprep.com`、Tiingo `tiingo.com`、Alpaca `alpaca.markets`(paper 账号,要 ID+Secret)。

## 定时运行（每交易日 3 次，按美东 ET）

GitHub Actions 在开盘后 **09:35**、盘中 **12:45**、收盘后 **16:05**（美东）扫描；工作流同时登记 EDT/EST 两套 UTC cron，并用 ET 守门避免夏冬令时重复。`state.json` 自动去重，同一日的每日催办不会重复推。

```bash
# crontab(注意:cron 用服务器本地时区,下面按服务器=美东 ET 计;非 ET 请换算)
# 开盘后 09:35 / 盘中 12:45 / 收盘后 16:05 ET（服务器须设 TZ=America/New_York）
35 9 * * 1-5 cd /path/to/ca_monitor && /usr/bin/python3 run.py >> data/cron.log 2>&1
45 12 * * 1-5 cd /path/to/ca_monitor && /usr/bin/python3 run.py >> data/cron.log 2>&1
5 16 * * 1-5 cd /path/to/ca_monitor && /usr/bin/python3 run.py >> data/cron.log 2>&1
```

> 服务器非美东时区时,建议设 `TZ=America/New_York` 或用 UTC 换算(ET 比 UTC 慢 4–5 小时)。
> 推送:`run.py` 已生成 `data/latest_digest.txt`,接邮件/Slack/Telegram 时在 `build()` 末尾把 digest 发出去即可。

## 推送到 Lark(飞书国际版)

用**自定义机器人 Webhook**,无需建应用:

1. Lark 里建一个群(或用现有群)→ 群设置 → **机器人** → **添加机器人** → **自定义机器人 (Custom Bot)**。
2. 复制 **Webhook 地址**(形如 `https://open.larksuite.com/open-apis/bot/v2/hook/xxxx`)。
3. 安全设置选 **签名校验** 最稳妥,复制它给的 **密钥(secret)**;(也可用关键词/IP 白名单,那样不需要 secret)。
4. 填进 `.env`:

```
LARK_WEBHOOK=https://open.larksuite.com/open-apis/bot/v2/hook/xxxx
LARK_SECRET=（开了签名校验才填,否则留空）
LARK_DASHBOARD_URL=https://你的面板地址/   # 可选,卡片底部按钮（Pages 根路径）
LARK_NOTIFY_EMPTY=0   # 1=没预警也推一条
LARK_REQUIRED=0       # 本地可选；GitHub Actions 生产固定为 1
```

之后每次 `python run.py` 跑完会自动把**临近预警 / 新发现 / 冲突 / 空缺 / 预测状态更新**整理成一张交互卡片推到群里(filing 带 SEC 原文链接,底部「打开面板」按钮)。`state.json` 去重,同一预警轮次不会重复推;稳定的预测观察不会每日刷屏，只有升级、改期或失效才推送。生产环境的 webhook 缺失或返回错误会直接让 Action 失败，且不推进去重状态，以便下次重试。单独测试推送:`python notify_lark.py`。

> 签名算法:以 `"{timestamp}\n{secret}"` 为 HMAC-SHA256 的 key、空消息体,base64;timestamp 需在服务器时间 1 小时内。

## 云端托管:GitHub Actions + GitHub Pages

`.github/workflows/monitor.yml` 已配好:每交易日 3 次（09:35 / 12:45 / 16:05 ET）自动抓取 → 核对 → 推 Lark → 把 `dashboard.html` 和 `site_data.json` 部署到 GitHub Pages（在线网页，自动更新）。调度直接使用 GitHub 原生 `timezone: America/New_York`，无需夏/冬令时双 cron 或运行时门禁，因此延迟排队不会再误跳过。Pages 数据缺失或 Lark 投递失败会让工作流变红；Lark 短暂失败时 Pages 仍会尝试刷新。

启用步骤(一次性):

1. **加密钥**:repo → Settings → Secrets and variables → **Actions** → New repository secret,逐个加:
   `ALPHAVANTAGE` `FMP` `TIINGO` `ALPACA_KEY_ID` `ALPACA_SECRET` `SEC_UA` `LARK_WEBHOOK`(开了签名校验再加 `LARK_SECRET`)。
2. **启用 Pages**:repo → Settings → **Pages** → Source 选 **GitHub Actions**。
3. (可选)加仓库变量 `LARK_DASHBOARD_URL` = 你的 Pages 网址(见下),Lark 卡片按钮就指向它。
4. **手动触发一次**:repo → Actions → CA Monitor → Run workflow。跑完后网页地址为
   `https://vancoder4-cyber.github.io/CA-Monitor/`。

> **提交到 GitHub 不会立即刷新 Pages 或 Lark**：工作流只在定时或手动 Run workflow 时运行。推送后请手动触发一次（或等待下一扫描窗口），并分别确认 `build` / `deploy` 和 Lark 投递结果；Railway 交互 Bot 也要确认已拉到同一提交/镜像。
> ⚠️ 公开 Pages = 网址公开可见,持仓清单会公开。要私有请改用 Cloudflare Pages/Netlify 加访问控制。

## CA问答助手 指令清单

群里 **@CA问答助手 + 关键词** 触发。指令的**唯一来源**是 `bot/cards.py` 的 `COMMANDS`(HELP_TEXT、关于卡片、指令解析都由它生成)。

顺序 = 用户动线 + 匹配优先级:先上手/元信息,再按紧迫度高→低。

| 指令 | 关键词 | 作用 |
|---|---|---|
| 关于 | 关于 / 介绍 / about | 这是什么、数据源、规则、更新时点 |
| 帮助 | 帮助 / help | 显示指令说明 |
| 最近更新 | 最近更新 / 更新 / changelog / 版本 | 最近 3 次版本更新(更多见网页) |
| 风险 | 风险 / 风控 / risk | 当日风控清单(拆股/并购退市/冲突 + 风控动作) |
| 今日 | 今日 / 今天 / today | T0 前后 24 小时的关键日(除息/登记/派发/宣告) |
| 新公告 | 新公告 / 公告 / announce | 最近 5 个宣告的事件(已派发完标「已结束」) |
| 本周 | 本周 / week | 未来 7 天的公司行动 |
| 临近催办 | 临近催办 / 催办 / 临近 / 待执行 | 已公告未发生的公司行动,按距除息天数排 + 催办文案(随时拉,不必等推送) |
| 观察预测 | 观察 / 预测 / 等待宣告 / watch | 标记或查看单源预测：`观察 CODE YYYY-MM-DD [备注]`；未证实前不进入执行催办，自动跟踪升级/改期/失效 |
| 日历 | 日历 / calendar / cal | 当月公司行动月历(图) |
| 覆盖 | 覆盖 / 资产 / 标的 / coverage | 各标的在现货/合约的覆盖情况 |
| 查代码 | @我 + 代码(如 AVGO) / 查代码 / 查 | 单标的全量:分红/拆股关键日(宣告/登记/除息/派发+距今)、重大事件(并购/退市)+SEC原文、风控动作、运营提醒;只发『查代码』看用法说明 |
| 确认 | 确认 / confirm / 已核对 | **人工放行异常**:`确认 CODE [正确值] [日期] [备注]` → 解除金额门禁、停报警、按你给的值显示,并**只追加不删**地写入留痕库(需配 GH_TOKEN)。同一标的有多条不同值的异常时**必须带日期**,如 `确认 AAPL 0.26 2026-08-11 已比对公司公告` |
| 留痕 | 留痕 / 审计 / 确认记录 / audit / log | **调取确认留痕**:谁在何时把哪条改成了什么值 + 核对来源 + 备注(可加代码只看某标的);离线表用 `tools/export_ack_log.py` 导 Excel |
| 需求提报 | 需求 / 提报 / 反馈 / 建议 | `需求 你的想法` → 追加到仓库 requests.md 供负责人迭代(需配 GH_TOKEN) |

### ⚠️ 维护规则:改指令必须四处同步(有检查机制)

**每次新增/修改指令,务必同步这四处,否则视为未完成:**

1. `bot/cards.py` 的 **`COMMANDS`**(唯一来源)——加/改条目;
2. `bot/bot.py` 的 **`on_message` dispatch**——加对应 `elif cmd == "<key>"` 分支;
3. 上面这张 **指令清单**(README);
4. 跑检查:**`python tools/check_commands.py`** —— 必须输出 `✅`。

`check_commands.py` 会校验 COMMANDS / bot.py 分发 / HELP_TEXT / README / bot README 是否一致；`check_surface_consistency.py` 会用 Visa 官方宣告 fixture 校验网页、推送、交互 Bot 和月历均有同一套官方 + 第三方链接。二者都由 CI 强制执行。

### ⚠️ 更新日志规则:每次 push 必须记一条

更新日志唯一来源是根目录 **`CHANGELOG.md`**(`run.py` 解析它发布到网页「更新日志」区 + 机器人 `最近更新` 指令)。

**每次 push 前**,在 `CHANGELOG.md` **最上面**加一条:

```
## 2026-06-20 · 本次改了啥(标题)
- 要点一(简洁)
- 要点二
```

机器人 `最近更新` 展示最新 3 条,更多跳网页;面板「🆕 更新日志」展示全部。`check_commands.py` 也会校验 `CHANGELOG.md` 至少有一条且可解析(CI 强制)。

## 免费源额度提醒(生产注意)

- **Alpha Vantage** 免费 25 次/天:67 支监控标的远超额,代码已限量(`av_limit=24`,只给前 24 支)+ 限流自动标「不可用」。生产建议升级或仅作补充。
- **抓取耗时**:67 支 × 8 并发，Action 时长会受源限流与网络情况影响。
- **FMP** 免费版对部分票返回 402(额度/覆盖限制),已按「不可用」处理。要全覆盖需付费档。
- **yfinance / Nasdaq / Tiingo / Alpaca** 实测对个股稳定全绿,是当前核对主力。

## 部署到 GitHub

```bash
git init && git add . && git commit -m "corporate actions monitor"
# 确认 .env 没被提交:
git status --ignored | grep .env     # 应显示在 Ignored 区
```
