# 公司行动预警面板(多源交叉核对)

盯住一篮子标的(**现货 62 + 合约 39，共 81 个覆盖资产，其中 75 个可监控证券**)的公司行动(分红 / 拆股·合股 / 并购 / 分拆 / 退市),**最多 8 类来源按各自覆盖与额度并行抓取 → 归一化 → 零容忍交叉核对 → 预测观察 / 报警 / 人工确认**。逻辑接近机构的 golden-copy 做法。

四个核心设计:
1. **零容忍**:同一事件多源比对,任一字段不一致或某源缺失就报警,**不做任何口径豁免**。
2. **金额门禁**:只有「多源交叉验证过且无冲突」的金额才是可执行确定值;有冲突或只有单源绝不作为确定值展示。未见宣告日的单源值仅以「预测」保留给跟踪，防止运营照着未核实的值执行。
3. **预测观察 + 人工介入闭环**:未宣告的单源预估按 30/14 天节奏推**数据核验提醒**，但不进入正式执行催办；同时跟踪升级/改期/失效。字段冲突和数据空缺则每次扫描重报、显示「已挂 N 天」,超期自动 @ 负责人。
4. **展示与合约操作分离**:公司行动仍正常报告；现金分红、送股、拆股、合股等只有估算价格影响严格 **>3%** 才触发合约操作，3% 或以下明确显示「合约：本次无需操作」。

产出:一屏看全的 HTML 面板(日历 + 预警 + 更新日志)+ Lark 推送 + @机器人问答。

> **运营、产品、风控与值班人员请直接使用：[全量操作手册](OPERATIONS_MANUAL.md)**。手册覆盖每日 SOP、全部 16 个问答指令、现货/合约处理、3% 门槛、人工确认、老虎 API 字段映射、发布维护和故障排查；另有可直接发群的 [群内简版说明](docs/CA监控_群briefing.md) 和 [人工确认清单](docs/CA_人工确认清单.md)。

### 分红的官方化与核对链接（所有展示面一致）

`run.py` 为每条分红预先生成同一份 `references`：**已逐项核验的官方本次公告/IR → SEC 本次宣告 8-K → 公司 IR 分红页 → SEC 公司备案**，并始终附 **StockAnalysis（交叉核对，可能滞后）**。网页、定时推送、单标的查询、临近催办、今日/本周、日历和新公告均消费这同一份数据；**Nasdaq 仅为采集源，绝不再作为唯一核对链接**。

已经确认公司正式宣告、但采集源尚未补齐字段时，在 `refs.json` 的 `official_event_overrides` 登记该事件的官方 URL、核验日期和已确认字段。它会参与零容忍核对；与采集源不一致仍会报警，不会静默覆盖。已退出现货范围的 V 2026-08-11 分红保留为本地历史回归 fixture，不会再被发布为当前预警或覆盖标的。

## 数据源（最多 8 类来源，3 类角色）

| 源 | 角色 | Key | 覆盖 |
|---|---|---|---|
| **yfinance** | 分红/拆股(历史) | 免 | 尽力覆盖 |
| **Nasdaq** | 分红(按票)+ 拆股(市场日历) | 免 | 尽力覆盖；仅采集，不作唯一核对页 |
| **Tiingo** | 分红/拆股交叉源 | 免费 token | 尽力覆盖 |
| **Alpaca** | 分红/拆股 + **并购/分拆/退市结构化** | 免费 key(ID+Secret) | 尽力覆盖；单源无宣告日只作预测 |
| **FMP** | 分红/拆股 | 免费 key | 部分票(免费版 402 限额) |
| **Alpha Vantage** | 分红/拆股 | 免费 key | 尽力(免费 25 次/天,易限流) |
| **SEC EDGAR** | 明确结构性 filing(权威) | 免 | 覆盖可监控的美股/ETF，8-K / 8-K/A、6-K / 6-K/A、S-4、25-NSE 等 |
| **FINX(TRKD-HS)** | 分红/拆股/并购(JWT) | `FINX_USER`+`FINX_PASS` | 已上线;供方接口仍在调整期。**未配置凭证则静默跳过,不影响其它源** |

> 关键设计:源被限流/付费墙时标「**不可用**」而非「空缺」,绝不把"没查到"误判成"源说没有"。
>
> **SEC filing 分流**：8-K 与 8-K/A 只有仅凭 Item 号就能确定的破产/接管、完成收购或资产处置、退市、证券权利变更、控制权变更自动进入公司行动流。`1.01 / 2.02 / 5.02 / 5.07 / 7.01 / 8.01` 等宽泛披露只留在 SEC 原文表，不凭 Item 号猜测公司行动。ARM / BABA / TSM 等外国发行人的 6-K / 6-K/A 也会进入原文审计表；只有文件名或 SEC 描述命中分红、拆合股、并购、退市等强提示时才进入「疑似相关·待核实」，普通 6-K 不触发业务风控或执行提醒。
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
  - 📅 **公司行动日历**:月历视图,分红/拆股/结构性事项按日期铺格;分红主块标「除息」、拆股/合股主块标「生效」，并展示登记/派发等关键日；悬停看完整日期，冲突红框、单源或条款待核实黄框
  - 🔔 **预警面板**:未来事件时间线 + 报警区(新发现/临近/冲突/空缺)+ 源健康矩阵
- `data/latest_digest.txt` —— 定时推送用的纯文本预警清单
- `data/state.json` —— 记录已见事件(新发现判定)与已触发预警轮次(去重)
- `data/forecast_watch.json` —— 人工标记的预测观察项；未获证实前只推核验提醒，不进入正式执行催办

## 报警逻辑

- **新发现**:本次出现、上次没见过的事件(近 30 天内)
- **临近提醒**:需要现货处理、命中合约操作门槛，或需要补齐公司行动条款/合约门槛数据的正式事件，进入距除息/生效日 **30 天**窗口知会一次，15–29 天安静，**≤14 天每天**提醒一次；单源预测只推数据核验并明确「勿执行」
- **未来跟踪事项**:已公告未发生的事件持续展示 + 倒计时；疑似结构性 filing 单独标「公司行动条款核验」、不计为正式事项。合约无需操作的事项不进入周期催办，未解决冲突只进入核验、不进入执行
- **预测观察**:单源且未见宣告日的预估，**会进入数据核验提醒，但不进入正式执行催办**；等待公司宣告或第二个独立源，改期/升级/失效也会主动推送
- **字段冲突(零容忍)**:≥2 源对同一事件的 除权日/登记日/派发日/金额/拆股比例 有任何差异
- **数据空缺**:近 200 天内,某个"在覆盖该票"的源缺了别的源有的事件

只对「近 200 天 + 未来」的事件做冲突/空缺判定,避免老历史的覆盖深度差异造成噪音。

每条事件展示完整关键日链:**宣告 · 登记 · 除息/生效 · 派发**。

### 产品处理口径（信息展示 ≠ 合约操作）

所有覆盖内公司行动仍正常采集、展示，并在新发现/新宣告时照常报告。对包含「合约」的标的，系统另行生成统一的产品动作结论：

- **现金分红**：影响率 = 每股现金分红毛额 ÷ 前一完整交易日的未调整收盘价。
- **送股**：根据每股新增股份比例估算理论除权价影响；**拆股/合股**：根据新旧股比例估算理论除权价变动。
- 上述类型统一使用严格 **>3%** 门槛：超过才显示「合约：需操作」；**≤3%** 显示「合约：本次无需操作」。
- **缺金额、比例、可靠参考价、币种/证券单位，或数值仍有冲突**：显示「合约：待核实」，不得默认判为无需操作，也不得作为执行指令。ADR/ADS 金额与美股报价单位不一致时同样 fail closed。
- **现货+合约**：两边结论分开。合约无需操作不影响现货成本基准、持仓及对账流程。

仅合约且无需操作的事项保留在信息流、日历和单标的查询中，但不进入 30/14 日重复执行催办，也不触发正式催办 @。结论从待核实/需操作变为无需操作时会单独推一次解除更新。行情快照跨运行保留 last-known-good；当前优先 Tiingo、回退 yfinance，统一取前一完整交易日收盘价。

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

异常 = 字段冲突 / 数据空缺。未见宣告日的单源预估走「预测观察」：按 30/14 天节奏推数据核验提醒，但不进入正式执行催办、不要求人工确认；持续等公司宣告或第二个独立源，预计日期/金额变化或预计日已过仍未证实也会主动推送。

1. **不做口径豁免** —— ADR 扣税、拆股回溯、四舍五入造成的差异照报。
2. **每次扫描都重报**,不确认就一直挂;推送与官网显示「**已挂 N 天**」。
3. 超过 `REVIEW_ESCALATE_DAYS`(默认 3 天)没人确认 → 推送顶部 **@ 负责人**升级。
4. **消解方式**:群里发 `确认 代码 正确值 日期`(如 `确认 AAPL 0.26 2026-08-11`；拆/合股保留完整比例，如 `确认 XYZ 1:10 2026-09-10`)
   → 以「代码 + 事件类型 + 日期」精确写入生效库和留痕库，异常列表即时标记；金额/比例门禁及 3% 产品结论由下一轮流水线结合币种、证券单位与参考价重算。没有具体事件、日期或有效值的宽泛确认不会放行。

## 配置(`config.py`)

- `SPOT_TICKERS` / `CONTRACT_TICKERS` —— 现货 **62 支** / 合约 **39 支**(含 ETF 与商品/海外)，去重后合计 **81** 个覆盖资产
- `TICKERS` —— 实际监控标的 **75 支**(覆盖内个股 + QQQ/EWY/DRAM/TQQQ/MVLL ETF；商品列入覆盖但不监控)
  - 代码格式坑:Berkshire B 类抓取 canonical ticker 必须写 **`BRK-B`**；Bot 同时接受 RFQ 输入 `BRKB` / `BRK.B`。`QNTX` 在 Bot 中会规范化为 `QNT`。
  - 业务已确认 `BBX` 即 `BB`（BlackBerry）；Bot 接受 `BBX`，数据供应商与公开页面继续使用 canonical ticker `BB`。
  - `BASELINE_NEW_TICKERS` —— 新标的首次纳入时,历史事件是否静默建基线。`False`(默认)= 照常推「新发现」(历史上一次大批量上新会刷屏但能看全);`True` = 记为已见但不推(不刷屏)。此前一次大批量上新实测:False→72 条,True→0 条
- `ALERT_HEADSUP_DAY` / `ALERT_DAILY_WITHIN` —— 30 天一次知会 / 14 天内每日催办；`ALERT_ROUNDS` 仅保留给兼容旧调用
- `CONTRACT_PRICE_IMPACT_THRESHOLD_PCT` —— 合约公司行动价格影响门槛，当前为严格 `>3%`；`CONTRACT_REFERENCE_PRICE_MAX_AGE_DAYS` 控制参考价过期门禁
- `GROUP_WINDOW_DAYS` —— 跨源归组时间窗(默认 5 天)
- API key —— **全部从 `.env` / 环境变量读取,代码里不留明文**:
  `FMP` / `ALPHAVANTAGE` / `TIINGO` / `ALPACA_KEY_ID` / `ALPACA_SECRET` / `SEC_UA` / `FINX_USER` / `FINX_PASS`(可选,FINX 第 8 源;`FINX_BASE` 可改 UAT)
- `GH_TOKEN` —— 细粒度 PAT(Contents 读写),供「确认 / 预测观察 / 备案结论 / 需求提报」写回仓库(配在 Railway)
- `LARK_WRITE_ALLOWED_OPEN_IDS` —— Railway Secret 中的操作员 open_id 白名单；确认、观察、备案结论和需求写回仅对白名单开放，未配置时全部写操作 fail closed

**一键触发 Action**:`./tools/trigger.sh`(触发 + 等跑完 + 核验网页刷新;需 `brew install gh && gh auth login`)。

**可维护文件(改完提交即可)**:`refs.json`(官方 IR / 已核验事件)、`data/filing_review_resolutions.json`(按稳定 event_id 记录 SEC 条款结论)、`OPERATIONS_MANUAL.md`(全量操作手册)、`CHANGELOG.md`(每次必记一条)、`UPDATE_CHECKLIST.md`(收尾检查清单)、`TODO.md`(内部技术待办/后续跟进)。现货/合约催办 @ 名单只放各自的 GitHub Secret；filing 生效库仅保存事件 ID、结论、时间和 SEC 来源，不保存 Lark open_id、姓名或自由备注；`requests.md` 会在首次「需求」提报时自动创建，正文公开可见但不保存提报人的 Lark 身份。

## 密钥与安全

- `.env` 含真实密钥,**已在 `.gitignore`,绝不要提交到 GitHub**。
- 部署到生产时,优先用平台的 Secrets / 环境变量注入,而不是把 `.env` 打进镜像。
- `LARK_ALERT_SPOT_MENTION_OPEN_IDS` / `LARK_ALERT_CONTRACT_MENTION_OPEN_IDS` 只能放 GitHub Actions Secret，不得写入 `refs.json`、Pages、日志或文档；旧 `LARK_ALERT_MENTION_OPEN_IDS` 只作迁移期逐组兜底。
- `LARK_WRITE_ALLOWED_OPEN_IDS` 只能放 Railway Secret，不得复用催办 @ 名单或发布到 Pages；发送人缺失、未配置或未命中均拒绝写回。
- 「需求提报」正文会匿名进入公开仓库，请勿填写客户、账号、密钥等敏感信息；确认/观察的审计身份仍需后续迁入私有存储，见 `TODO.md`。
- 免费 key 申请:Alpha Vantage `alphavantage.co/support/#api-key`、FMP `site.financialmodelingprep.com`、Tiingo `tiingo.com`、Alpaca `alpaca.markets`(paper 账号,要 ID+Secret)。

## 定时运行（美东周一至周五 3 次，按 ET）

GitHub Actions 在开盘后 **09:35**、盘中 **12:45**、收盘后 **16:05**（美东）扫描；工作流直接使用 `timezone: America/New_York`，夏冬令时自动换算，不再依赖双 UTC cron 或运行时门禁。`state.json` 自动去重，同一日的每日催办不会重复推。

> 当前 workflow 只按周一至周五调度，尚未接入美股交易所节假日日历；休市节假日仍可能运行。

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
LARK_ALERT_SPOT_MENTION_OPEN_IDS=ou_xxx,ou_yyy      # 仅放 Secret；现货正式催办负责人
LARK_ALERT_CONTRACT_MENTION_OPEN_IDS=ou_xxx,ou_yyy  # 仅放 Secret；合约正式催办负责人
LARK_ALERT_MENTION_OPEN_IDS=                         # 可选；旧版全局名单，仅作迁移兜底
LARK_WRITE_ALLOWED_OPEN_IDS=ou_xxx,ou_yyy  # 仅放 Railway Secret；可执行确认/观察/备案结论/需求写回的操作员
```

之后每次 `python run.py` 跑完会自动把**执行催办 / 公司行动条款核验 / 合约门槛核验 / 单源核验 / 新发现 / 冲突 / 空缺 / 预测状态及合约结论更新**整理成一张交互卡片推到群里(filing 带 SEC 原文链接,底部「打开面板」按钮)。疑似结构性 filing 只要求打开 SEC 原文核对条款，核实前不得执行或触发正式 @；公司行动照常报告，但仅合约且确认价格影响 ≤3% 的事项不会进入重复催办或触发 @，并明确写「合约：本次无需操作」。`state.json` 去重：30 天窗口首次知会一次，14 天内每天最多一次，各核验/执行通道独立计数。生产环境的 webhook 缺失或返回错误会直接让 Action 失败，且不推进去重状态，以便下次重试。单独测试推送:`python notify_lark.py`。

> 签名算法:以 `"{timestamp}\n{secret}"` 为 HMAC-SHA256 的 key、空消息体,base64;timestamp 需在服务器时间 1 小时内。

## 云端托管:GitHub Actions + GitHub Pages

`.github/workflows/monitor.yml` 已配好:美东周一至周五 3 次（09:35 / 12:45 / 16:05 ET）自动抓取 → 核对 → 推 Lark → 把 `dashboard.html` 和 `site_data.json` 部署到 GitHub Pages（在线网页，自动更新）。调度直接使用 GitHub 原生 `timezone: America/New_York`，无需夏/冬令时双 cron 或运行时门禁，因此延迟排队不会再误跳过。状态恢复、抓取、Lark 投递、公开数据校验任一步失败都会让工作流变红并保留上一版 Pages；但 Lark 投递发生在后续公开快照校验和 cache 保存之前，因此若后续步骤失败，群里可能已收到卡片。重跑前必须先核对群消息，避免重复推送。

Pages `data.json` 使用 **schema v4**，包含 `generated_at_utc`、`valid_until_utc`、`source_sha`、`run_id` 与 `delivery_status`。v4 固化了公司行动条款核验、合约门槛核验与执行催办的分流语义。网站顶部显示生成时间、schema、commit 和新鲜度提示，完整 provenance 应直接核对 `data.json`；问答助手会对版本、必要字段和时效做硬门禁。网站时间异常时会显示红色警告但仍可能保留旧内容，操作员必须停止采信；Bot 则停止输出「无风险 / 无需操作」等业务结论。

启用步骤(一次性):

1. **加密钥**:repo → Settings → Secrets and variables → **Actions** → New repository secret,逐个加:
   `ALPHAVANTAGE` `FMP` `TIINGO` `ALPACA_KEY_ID` `ALPACA_SECRET` `SEC_UA` `LARK_WEBHOOK`(开了签名校验再加 `LARK_SECRET`)；正式催办需要 @ 时另加 `LARK_ALERT_SPOT_MENTION_OPEN_IDS` / `LARK_ALERT_CONTRACT_MENTION_OPEN_IDS`。现货事件只取现货组、合约事件只取合约组、两边覆盖的标的合并两组并去重。
2. **启用 Pages**:repo → Settings → **Pages** → Source 选 **GitHub Actions**。
3. (可选)加仓库变量 `LARK_DASHBOARD_URL` = 你的 Pages 网址(见下),Lark 卡片按钮就指向它。
4. **手动触发一次**:repo → Actions → CA Monitor → Run workflow。跑完后网页地址为
   `https://vancoder4-cyber.github.io/CA-Monitor/`。

> **合入 `main` 会自动刷新 Pages 与 Lark**：生产工作流同时保留定时和手动触发。只推送功能分支不会更新生产；合并后仍要分别确认 `build` / `deploy`、Lark 投递与 Railway 交互 Bot 已拉到同一提交/镜像。
> ⚠️ 公开 Pages = 网址公开可见，资产覆盖、公司行动与运行版本信息都会公开。要私有请改用带访问控制的托管方式。

## CA问答助手 指令清单

群里 **@CA问答助手 + 关键词** 触发。指令的**唯一来源**是 `bot/cards.py` 的 `COMMANDS`(HELP_TEXT、关于卡片、指令解析都由它生成)。

顺序 = 用户动线 + 匹配优先级:先上手/元信息,再按紧迫度高→低。

| 指令 | 关键词 | 作用 |
|---|---|---|
| 关于 | 关于 / 介绍 / about | 这是什么、数据源、规则、更新时点 |
| 帮助 | 帮助 / help | 显示指令说明 |
| 最近更新 | 最近更新 / 更新 / changelog / 版本 | 最近 3 次版本更新(更多见网页) |
| 风险 | 风险 / 风控 / risk | 当日风控清单，展示现货/合约独立动作结论 |
| 今日 | 今日 / 今天 / today | T0 前后 24 小时的关键日(分红除息、拆股/合股生效、登记、派发、宣告) |
| 新公告 | 新公告 / 公告 / announce | 最近 5 个宣告的事件(已派发完标「已结束」) |
| 本周 | 本周 / week | 未来 7 个自然日（含今天），按除息/生效日统计正式事件并去重；预测不计 |
| 临近催办 | 临近催办 / 催办 / 临近 / 待执行 | 距除息/生效 0–14 天的执行催办 + 数据核验；合约 ≤3% 不进催办，与本周 7 天窗口分开 |
| 观察预测 | 观察 / 预测 / 等待宣告 / watch | 标记或查看单源预测：`观察 CODE YYYY-MM-DD [备注]`；按 30/14 天节奏推核验提醒，未证实勿执行 |
| 日历 | 日历 / calendar / cal | 当月公司行动月历(图) |
| 覆盖 | 覆盖 / 资产 / 标的 / coverage | 各标的在现货/合约的覆盖情况 |
| 查代码 | @我 + 代码(如 AVGO) / 查代码 / 查 | 单标的全量:分红/拆股关键日(宣告/登记/除息或生效/派发+距今)、已确认结构性行动与疑似条款核验+SEC原文、风控动作、运营提醒;只发『查代码』看用法说明 |
| 备案结论 | 备案结论 / 确认备案 / 排除备案 | 按卡片中的完整 `event_id` 把 SEC 条款核验结案：`确认备案 EVENT_ID` 或 `排除备案 EVENT_ID`；也可写 `备案结论 公司行动|普通备案 EVENT_ID`。公开 Git 只留匿名业务结论，不保存操作员身份或自由备注(需 GH_TOKEN) |
| 确认 | 确认 / confirm / 已核对 | `确认 CODE 正确值 日期 [备注]` → 按代码、类型和日期精确写入生效库及留痕库；异常列表即时标记，金额/3% 结论由下一轮流水线重算(需配 GH_TOKEN)。如 `确认 AAPL 0.26 2026-08-11 已比对公司公告`；拆/合股必须使用完整 `新股数:旧股数`，如 `确认 XYZ 1:10 2026-09-10` |
| 留痕 | 留痕 / 审计 / 确认记录 / audit / log | 调取金额确认、预测观察和匿名备案结论记录(可加代码只看某标的)；离线表用 `tools/export_ack_log.py` 导 Excel |
| 需求提报 | 需求 / 提报 / 反馈 / 建议 | `需求 你的想法` → 以匿名编号追加到公开仓库 `requests.md`；请勿填写敏感信息(需配 GH_TOKEN) |

### ⚠️ 维护规则:改指令必须六处同步(有检查机制)

**每次新增/修改指令,务必同步这六处,否则视为未完成:**

1. `bot/cards.py` 的 **`COMMANDS`**(唯一来源)——加/改条目;
2. `bot/bot.py` 的 **`on_message` dispatch**——加对应 `elif cmd == "<key>"` 分支;
3. 上面这张 **指令清单**(README);
4. `bot/README.md` 的 Bot 指令表；
5. `OPERATIONS_MANUAL.md` 的完整指令表；
6. `CHANGELOG.md` 的本次用户可见变化；

最后跑检查:**`python tools/check_commands.py`** —— 必须输出 `✅`。

`check_commands.py` 会精确校验 COMMANDS、bot.py 分发、HELP_TEXT 和操作手册第 9 节的指令集合，并确认 README / Bot README 提及全部指令；说明文字和关键词变化仍须人工按 `UPDATE_CHECKLIST.md` 复核。`check_surface_consistency.py` 会用 Visa 官方宣告 fixture 校验网页、推送、交互 Bot 和月历均有同一套官方 + 第三方链接。二者都由 CI 强制执行。

### ⚠️ 更新日志规则:每次修复完成时必须立即记录

更新日志唯一来源是根目录 **`CHANGELOG.md`**(`run.py` 解析它发布到网页「更新日志」区 + 机器人 `最近更新` 指令)。

任何 bug 修复、用户可见行为变更、规则/配置/数据修订或文案调整，都必须在**同一任务完成时**写入 `CHANGELOG.md` **最上面**；不得等到 push 或部署时再补。只读查询且仓库没有变化时可免。

```
## 2026-06-20 · 本次改了啥(标题)
- 要点一(简洁)
- 要点二
```

机器人 `最近更新` 展示最新 3 条,更多跳网页;面板「🆕 更新日志」展示全部。链路是 `CHANGELOG.md` → `run.py build` → Pages `data.json` → Bot，因此写入 Markdown 只代表本地已记录；发布类任务还必须在部署后确认生产 `data.json.changelog[0]` 是本次条目。`site_data.json` 是构建产物，不手工改。`check_commands.py` 也会校验 `CHANGELOG.md` 至少有一条且可解析(CI 强制)。

## 免费源额度提醒(生产注意)

- **Alpha Vantage** 免费 25 次/天:75 支监控标的远超额,代码已限量(`av_limit=24`,只给前 24 支)+ 限流自动标「不可用」。生产建议升级或仅作补充。
- **抓取耗时**:75 支 × 8 并发，Action 时长会受源限流与网络情况影响。
- **FMP** 免费版对部分票返回 402(额度/覆盖限制),已按「不可用」处理。要全覆盖需付费档。
- **yfinance / Nasdaq / Tiingo / Alpaca** 实测对个股稳定全绿,是当前核对主力。

## 部署到 GitHub

```bash
git init && git add . && git commit -m "corporate actions monitor"
# 确认 .env 没被提交:
git status --ignored | grep .env     # 应显示在 Ignored 区
```
