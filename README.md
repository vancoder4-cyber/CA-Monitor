# 公司行动预警面板(多源交叉核对)

盯住一篮子标的的公司行动(分红 / 拆股 / 并购 / 分拆 / 退市),**多源并行抓取 → 归一化 → 零容忍交叉核对 → 报警**,产出一屏看全的 HTML 面板 + 文本预警清单。逻辑接近机构的 golden-copy 做法:同一事件多源比对,缺失或字段不一致就报警。

## 数据源(7 主力 + FINX 接入中,3 类角色)

| 源 | 角色 | Key | 覆盖 |
|---|---|---|---|
| **yfinance** | 分红/拆股(历史) | 免 | 24/24 稳定 |
| **Nasdaq** | 分红(按票)+ 拆股(市场日历) | 免 | 24/24 稳定 |
| **Tiingo** | 分红/拆股交叉源 | 免费 token | 24/24 稳定 |
| **Alpaca** | 分红/拆股 + **并购/分拆/退市结构化** | 免费 key(ID+Secret) | 24/24 稳定 |
| **FMP** | 分红/拆股 | 免费 key | 部分票(免费版 402 限额) |
| **Alpha Vantage** | 分红/拆股 | 免费 key | 尽力(免费 25 次/天,易限流) |
| **SEC EDGAR** | 并购/退市 filing(权威) | 免 | 24/24,8-K/S-4/25-NSE 等 |
| **FINX(TRKD-HS)** | 分红/拆股/并购(JWT) | `FINX_USER`+`FINX_PASS` | 接入中:供方接口约 2 周内调整、demo 阶段;**配置凭证后才启用,留空自动跳过** |

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
- `dashboard.html` —— **单页站点,顶部标签切换两个视图**:
  - 📅 **公司行动日历**:月历视图,分红/拆股/并购按日期铺格;每个事件标除息(主块带金额)/ 登记 / 派发三个关键日,悬停看完整日期;冲突红框、单源黄框
  - 🔔 **预警面板**:未来事件时间线 + 报警区(新发现/临近/冲突/空缺)+ 源健康矩阵
- `data/latest_digest.txt` —— 定时推送用的纯文本预警清单
- `data/state.json` —— 记录已见事件(新发现判定)与已触发预警轮次(去重)

## 报警逻辑

- **新发现**:本次出现、上次没见过的事件(近 30 天内)
- **临近预警**:距除权日 `30/14/7/3/1` 天各触发一轮(去重,每轮只报一次)
- **字段冲突(零容忍)**:≥2 源对同一事件的 除权日/登记日/派发日/金额/拆股比例 有任何差异
- **数据空缺**:近 200 天内,某个"在覆盖该票"的源缺了别的源有的事件

只对「近 200 天 + 未来」的事件做冲突/空缺判定,避免老历史的覆盖深度差异造成噪音。

## 配置(`config.py`)

- `SPOT_TICKERS` / `CONTRACT_TICKERS` —— 现货 24 个股 / 合约 22(含 ETF 与商品/海外)
- `TICKERS` —— 实际监控标的 **27 支**(现货 24 个股 + QQQ/EWY/DRAM 三个 ETF;商品/海外列入覆盖但不监控)
- `ALERT_ROUNDS` —— 预警节奏 `[30,14,7,3,1]`
- `GROUP_WINDOW_DAYS` —— 跨源归组时间窗(默认 5 天)
- API key —— **全部从 `.env` / 环境变量读取,代码里不留明文**:
  `FMP` / `ALPHAVANTAGE` / `FINNHUB` / `TIINGO` / `ALPACA_KEY_ID` / `ALPACA_SECRET` / `SEC_UA` / `FINX_USER` / `FINX_PASS`(可选,FINX 第 8 源;`FINX_BASE` 可改 UAT)
- `GH_TOKEN` —— 细粒度 PAT(Contents 读写),供「确认 / 需求提报」写回仓库(配在 Railway)

**可维护文件(改完提交即可)**:`refs.json`(IR 分红页 + 催办 @ 名单)、`CHANGELOG.md`(每次必记一条)、`UPDATE_CHECKLIST.md`(收尾检查清单)、`requests.md`(需求自动汇总)。

## 密钥与安全

- `.env` 含真实密钥,**已在 `.gitignore`,绝不要提交到 GitHub**。
- 部署到生产时,优先用平台的 Secrets / 环境变量注入,而不是把 `.env` 打进镜像。
- 免费 key 申请:Alpha Vantage `alphavantage.co/support/#api-key`、FMP `site.financialmodelingprep.com`、Tiingo `tiingo.com`、Alpaca `alpaca.markets`(paper 账号,要 ID+Secret)。

## 定时运行(盘前 + 收盘,T0 扫描)

每个交易日跑两次:盘前抓「已 announce 未发生」的临近预警,收盘后抓当天「新 announce」。`state.json` 自动去重,同一预警轮次不会重复推。

```bash
# crontab(注意:cron 用服务器本地时区,下面按服务器=美东 ET 计;非 ET 请换算)
# 盘前 08:00 ET
0 8 * * 1-5 cd /path/to/ca_monitor && /usr/bin/python3 run.py >> data/cron.log 2>&1
# 收盘后 18:00 ET
0 18 * * 1-5 cd /path/to/ca_monitor && /usr/bin/python3 run.py >> data/cron.log 2>&1
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
LARK_DASHBOARD_URL=https://你的面板地址/dashboard.html   # 可选,卡片底部按钮
LARK_NOTIFY_EMPTY=0   # 1=没预警也推一条
```

之后每次 `python run.py` 跑完会自动把**临近预警 / 新发现 / 冲突 / 空缺**整理成一张交互卡片推到群里(filing 带 SEC 原文链接,底部「打开面板」按钮)。`state.json` 去重,同一预警轮次不会重复推;无预警默认不推(避免刷屏)。单独测试推送:`python notify_lark.py`。

> 签名算法:以 `"{timestamp}\n{secret}"` 为 HMAC-SHA256 的 key、空消息体,base64;timestamp 需在服务器时间 1 小时内。

## 云端托管:GitHub Actions + GitHub Pages

`.github/workflows/monitor.yml` 已配好:盘前/收盘各跑一次,自动抓取 → 核对 → 推 Lark → 把 `dashboard.html` 部署到 GitHub Pages(在线网页,自动更新)。

启用步骤(一次性):

1. **加密钥**:repo → Settings → Secrets and variables → **Actions** → New repository secret,逐个加:
   `ALPHAVANTAGE` `FMP` `FINNHUB` `TIINGO` `ALPACA_KEY_ID` `ALPACA_SECRET` `SEC_UA` `LARK_WEBHOOK`(开了签名校验再加 `LARK_SECRET`)。
2. **启用 Pages**:repo → Settings → **Pages** → Source 选 **GitHub Actions**。
3. (可选)加仓库变量 `LARK_DASHBOARD_URL` = 你的 Pages 网址(见下),Lark 卡片按钮就指向它。
4. **手动触发一次**:repo → Actions → CA Monitor → Run workflow。跑完后网页地址为
   `https://vancoder4-cyber.github.io/CA-Monitor/`。

> 定时用 UTC;ET 夏令时已对应 12:00/22:00 UTC,冬令时改成 13:00/23:00。
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
| 日历 | 日历 / calendar / cal | 当月公司行动月历(图) |
| 覆盖 | 覆盖 / 资产 / 标的 / coverage | 各标的在现货/合约的覆盖情况 |
| 查代码 | @我 + 代码(如 AVGO) / 查代码 / 查 | 单标的全量:分红/拆股关键日(宣告/登记/除息/派发+距今)、重大事件(并购/退市)+SEC原文、风控动作、运营提醒;只发『查代码』看用法说明 |
| 确认 | 确认 / confirm / 已核对 | 人工确认冲突:`确认 CODE [正确值]` → 停报警 + 网页 finalize(需配 GH_TOKEN) |
| 需求提报 | 需求 / 提报 / 反馈 / 建议 | `需求 你的想法` → 追加到仓库 requests.md 供负责人迭代(需配 GH_TOKEN) |

### ⚠️ 维护规则:改指令必须四处同步(有检查机制)

**每次新增/修改指令,务必同步这四处,否则视为未完成:**

1. `bot/cards.py` 的 **`COMMANDS`**(唯一来源)——加/改条目;
2. `bot/bot.py` 的 **`on_message` dispatch**——加对应 `elif cmd == "<key>"` 分支;
3. 上面这张 **指令清单**(README);
4. 跑检查:**`python tools/check_commands.py`** —— 必须输出 `✅`。

`check_commands.py` 会校验 COMMANDS / bot.py 分发 / HELP_TEXT / README 四处是否一致,不一致就报出差异并以非零码退出;**CI(monitor.yml)里也会自动跑这个检查**,不一致会让 Action 失败,从而强制同步。

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

- **Alpha Vantage** 免费 25 次/天:跑两次 ×27 支会远超额,代码已限量 + 限流自动标「不可用」。生产建议升级或仅作补充。
- **FMP** 免费版对部分票返回 402(额度/覆盖限制),已按「不可用」处理。要全覆盖需付费档。
- **yfinance / Nasdaq / Tiingo / Alpaca** 实测对个股稳定全绿,是当前核对主力。

## 部署到 GitHub

```bash
git init && git add . && git commit -m "corporate actions monitor"
# 确认 .env 没被提交:
git status --ignored | grep .env     # 应显示在 Ignored 区
```
