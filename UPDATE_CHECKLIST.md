# 更新收尾检查清单

目的：每次改动后验证 **GitHub 代码、Pages、定时推送、交互 Bot、文档和审计写回** 是同一版本与同一口径。不能只看语法、也不能只看网页。

## 1. 先判定影响面

| 改了什么 | 必须同步检查 |
|---|---|
| 指令 / Bot 文案 | `bot/cards.py`、`bot/bot.py`、根 `README.md`、`bot/README.md`、`OPERATIONS_MANUAL.md`、`CHANGELOG.md` |
| 事件字段 / 取值 / 门禁 | `run.py` 产出 ↔ `report.py` 网页 ↔ `notify_lark.py` 推送 ↔ `bot/cards.py` 交互卡 ↔ 月历 / `calendar_events` |
| 合约公司行动 / 3% 门槛 / 参考价 | `contract_policy.py`、行情快照缓存、pending/rounds、网页、定时 Lark、Bot 今日/本周/新公告/临近/日历/查代码、`CHANGELOG.md` |
| SEC filing 分类 / 事件相关性 | `config.describe_8k`、`sources._sec_filing_note_relevance`、`run.py` new/conflict/gap/calendar、SEC 原文表、网站、定时 Lark、Bot 风险/今日/本周/临近/查代码/PNG 日历 |
| 分红核对链接 / 官方宣告 | `refs.json`、单标的查询、今日/本周、临近催办、预测、日历、公告、网页、定时推送、确认留痕 |
| 标的数 / 代码别名 / 数据源 / 时点 | `config.py`、Pages `ticker_aliases`、根 README、`about_card`、`OPERATIONS_MANUAL.md`、群 briefing、workflow |
| Railway / Bot 运行方式 | `bot/README.md`、环境变量、心跳、生产验证步骤 |
| 快照 schema / 发布 / 状态缓存 | `run.py` provenance、网站/Bot 时效门禁、公开数据脱敏、Actions cache、Pages 原子发布、`tools/trigger.sh` |
| 写回 / 审计 / 需求提报 | 核心文件与留痕文件是否都成功、失败文案、公开仓库隐私边界、Bot 卡片与 README |
| 写操作权限 | `filing_resolve / confirm / forecast / request` 在取快照和写回前统一校验 Railway Secret `LARK_WRITE_ALLOWED_OPEN_IDS`；查询与审计不受影响 |

## 2. 分红引用契约（尤其容易漂移）

每一条分红都由 `run.py.attach_event_references()` 生成，消费端不得自己拼回退链接。

1. 官方顺序：已核验的官方本次公告/IR → 精确 8-K → 公司 IR 分红页 → SEC 公司备案。
2. 始终附 `StockAnalysis`，标签必须写明「交叉核对，可能滞后」。它不能作为正式化依据。
3. Nasdaq 只可作为采集源/健康矩阵，不能成为唯一分红核对链接。
4. 已核验官方事件填 `refs.json.official_event_overrides`；须有 URL、核验日期、事件字段。它仍参与零容忍冲突检测。
5. 所有分红路径都要核：**网页未来时间线 / 预警 / 月历、定时 Lark 推送、Bot 查代码 / 今日 / 本周 / 日历 / 临近催办 / 新公告 / 预测、审计写回来源**。

## 3. 状态与金额门禁

- 未见宣告日且单源：`预测观察·不执行`；按 30 天首次知会、14 天内每日推送**数据核验提醒**，但不能进入正式执行催办或运营动作。
- 有官方宣告或双源确认，但仍有未确认冲突：保持异常，不能进入催办。
- 只有一个采集源的金额：默认门禁；逐项核验的 `CompanyIR` 只有在自身带有对应金额/比例时才能验证该数值，只有日期的官方覆盖不能替供应商单源金额背书。
- `reconcile.pick_value()` 是字段取值唯一真相；检查是否又出现了 `next((v.get(...` 的平行取值逻辑。
- 公司行动展示与合约操作必须分离：现金分红、送股、拆股、合股等均按估算价格影响判断；`>3%` 才是 `required`，恰好 3% 和以下均为 `not_required`；缺价/日期、币种或证券单位不一致、过期价、未过金额门禁为 `review`，绝不能误写无需操作。
- contract-only + `not_required` 仍应出现在日历/新公告/查代码，并明确「合约：本次无需操作」，但不得进入 `rounds` 或触发正式 @；现货+合约则保留现货催办。
- SEC 8-K / 8-K/A 只有可仅凭 Item 代码确定的结构性事项进入公司行动流；宽泛的 1.01 / 2.02 / 5.02 / 5.07 / 7.01 / 8.01 保留在 SEC 原文表但必须被 new/conflict/gap/calendar、网站、Lark 和 Bot 各入口过滤。6-K / 6-K/A 默认只留原文审计；只有强公司行动元数据提示才进入「公司行动条款核验」，且即使标的含现货也不得变成执行催办或正式 @。检查 Distribution Agreement、合并财务结果、债券赎回三个负例不会误入。
- 公司行动条款核验必须能按完整稳定 `event_id` 用 `确认备案` / `排除备案` 结案；同日多份文件不可宽匹配。仅分红提示的 6-K 只有在 ticker、唯一事件和申报日=正式分红宣告日同时满足时才自动并入证据链；未解决项超过 30 天只发一次「未决归档、停止日报」通知，绝不能写成无需操作。结案/关联/归档都要在网站、digest、Lark 与 Bot 显示一次且不重复。
- 所有事件日文案按类型验证：现金/送股分红用「除息」，拆股/合股用「生效」，filing 用「事件日」；网页、digest、定时 Lark、Bot 新公告/临近/查代码和旧 payload 防御层必须一致。

## 4. 本地自动检查（必须全绿）

```bash
python3 tools/check_commands.py
python3 -m unittest discover -s tests -v
python3 tools/check_surface_consistency.py
python3 -m py_compile run.py reconcile.py contract_policy.py sources.py report.py notify_lark.py bot/cards.py bot/ack.py bot/bot.py
if test -s data/state.json; then python3 tools/validate_state.py data/state.json; else echo "local state absent; production Action will restore and validate it"; fi
bash -n tools/trigger.sh
env LARK_WEBHOOK='' LARK_SECRET='' LARK_REQUIRED=0 python3 run.py build  # 隔离 checkout；仍可能更新本地 state
python3 tools/validate_public_snapshot.py site_data.json
```

`check_surface_consistency.py` 使用历史 Visa 官方 IR fixture，并断言当前 RFQ 的集合、范围门禁、Pages/Bot 代码识别与临近催办截断提示；该 fixture 不代表 V 仍在当前支持范围。修改引用契约、预测逻辑、标的范围或渲染器时，必须先更新该测试。

`data/state.json` 不再跟踪到 Git，因此清洁 checkout 没有该文件是正常状态。本地只在已恢复非空状态时运行校验；生产必须由 GitHub Actions 的「恢复核心去重状态 → 验证核心去重状态」步骤 fail closed。

本地构建前必须显式清空 Lark 变量，避免根目录 `.env` 触发真实群推送；即便关闭 Lark，build 仍可能推进本地 state，必须使用隔离 checkout 或先备份。

## 5. 文档与提交

- [ ] 本次每一项修复、用户可见变更、规则/配置/数据修订或文案调整，都已在完成时立即写入 `CHANGELOG.md` 顶部；这是任务完成条件，不得等到 push 再补。只读查询且仓库无变化时可免。
- [ ] 最终交付前检查 `git diff -- CHANGELOG.md`，确认条目使用真实当天日期并准确覆盖本次变更。
- [ ] 根 README、`bot/README.md`、`OPERATIONS_MANUAL.md`、`TODO.md` 与本次事实一致；运营文档和群 briefing 同步。
- [ ] `refs.json` JSON 格式有效；不提交密钥、webhook、PAT 或 state/cache 临时文件。
- [ ] `data/state.json` 保持未跟踪并由独立 cache 持久化；生产恢复不到非空历史 state 时必须 fail closed，不能整批重放。
- [ ] 公开 Pages 快照通过递归脱敏检查；现货/合约催办 open_id 只在 `LARK_ALERT_SPOT_MENTION_OPEN_IDS` / `LARK_ALERT_CONTRACT_MENTION_OPEN_IDS` Secret（旧全局 Secret 仅作迁移兜底），需求 commit 标题不含原文或身份。
- [ ] Railway 已配置 `LARK_WRITE_ALLOWED_OPEN_IDS`；验证授权账号可写、未授权/缺 sender/未配置均拒绝，且拒绝回执不泄露白名单。
- [ ] 检查 `git diff`，确保没有将历史审计日志当作“修复”回写。

## 6. 发布后验收（四个独立面）

1. **GitHub Actions**：合入 `main` 会自动触发生产刷新（仍可手动 Run workflow 或等待 ET 扫描窗口）；分别确认 state 恢复校验、全量测试、Lark 投递、公开快照校验、`build` / `deploy`，不能只看 workflow 总体颜色。只推送功能分支不会刷新生产。
   - 若失败发生在 Lark 投递之后，群里可能已经收到消息但 cache/Pages 尚未保存；重跑前先核对群消息。若仅 `deploy` 失败，优先只重跑失败 job，避免整轮重复推送。
2. **Pages**：打开根地址并检查 `data.json` 的 `schema_version=4`、`source_sha=main HEAD`、`run_id=本次 Action`、`delivery_status`、`generated_at_utc < valid_until_utc`；确认 `changelog[0]` 等于本次顶部条目后，才可称 Bot「最近更新」已刷新。资产覆盖应为现货 62 / 合约 39 / 共 81 / 监控 75，`ticker_aliases` 应包含 `BBX → BB`。不要手工修改构建产物 `site_data.json`。
3. **定时推送**：下一次有内容的运行检查官方链接、第三方链接、预测/催办状态和 @ 名单；单源、公司行动条款与合约门槛三类核验应分别命名并明确「核验、勿执行」，且不能触发正式催办 @；合约 ≤3% 应正常首报并写「本次无需操作」，但不得进入 30/14 日重复催办；15–29 天静默期不应收到只有统计没有明细的卡片。
4. **Railway Bot**：先发 `关于`，确认数据 commit 与 Bot build commit 相同；测试 `帮助`、`风险`、`查 AAPL`、`查 BBX`（应返回 `BB`）、`查 BRK-B`、`查 SKHY`、`临近催办`、`观察预测`、`最近更新`。用 fixture 验证 `确认备案` / `排除备案` 精确 event_id 路由与冲突输入 fail closed；不要为了 smoke 修改真实事件结论。另用过期/坏 schema fixture 验证只返回红色故障卡，不得假报全绿；用授权/未授权账号各测一次写操作，未授权路径不得调用任何 GitHub 写回。Pages 更新不代表 Railway 已更新。

## 一句话流程

判定影响面 → 统一事件数据 → 自动检查 → 文档/CHANGELOG → PR 合入 main → 自动生产 workflow → 同时验收 Actions、Pages、Lark 推送与 Railway Bot。
