# 更新收尾检查清单

目的：每次改动后验证 **GitHub 代码、Pages、定时推送、交互 Bot、文档和审计写回** 是同一版本与同一口径。不能只看语法、也不能只看网页。

## 1. 先判定影响面

| 改了什么 | 必须同步检查 |
|---|---|
| 指令 / Bot 文案 | `bot/cards.py`、`bot/bot.py`、根 `README.md`、`bot/README.md`、`CHANGELOG.md` |
| 事件字段 / 取值 / 门禁 | `run.py` 产出 ↔ `report.py` 网页 ↔ `notify_lark.py` 推送 ↔ `bot/cards.py` 交互卡 ↔ 月历 / `calendar_events` |
| 合约公司行动 / 3% 门槛 / 参考价 | `contract_policy.py`、行情快照缓存、pending/rounds、网页、定时 Lark、Bot 今日/本周/新公告/临近/日历/查代码、`CHANGELOG.md` |
| 分红核对链接 / 官方宣告 | `refs.json`、单标的查询、今日/本周、临近催办、预测、日历、公告、网页、定时推送、确认留痕 |
| 标的数 / 代码别名 / 数据源 / 时点 | `config.py`、Pages `ticker_aliases`、根 README、`about_card`、操作手册、群 briefing、workflow |
| Railway / Bot 运行方式 | `bot/README.md`、环境变量、心跳、生产验证步骤 |

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

## 4. 本地自动检查（必须全绿）

```bash
python3 tools/check_commands.py
python3 -m unittest discover -s tests -v
python3 tools/check_surface_consistency.py
python3 -m py_compile run.py reconcile.py contract_policy.py sources.py report.py notify_lark.py bot/cards.py bot/ack.py bot/bot.py
python3 run.py build       # 有缓存时验证真实产物；会跳过未配置的 Lark webhook
```

`check_surface_consistency.py` 使用历史 Visa 官方 IR fixture，并断言当前 RFQ 的集合、范围门禁、Pages/Bot 代码识别与临近催办截断提示；该 fixture 不代表 V 仍在当前支持范围。修改引用契约、预测逻辑、标的范围或渲染器时，必须先更新该测试。

## 5. 文档与提交

- [ ] 本次每一项修复、用户可见变更、规则/配置/数据修订或文案调整，都已在完成时立即写入 `CHANGELOG.md` 顶部；这是任务完成条件，不得等到 push 再补。只读查询且仓库无变化时可免。
- [ ] 最终交付前检查 `git diff -- CHANGELOG.md`，确认条目使用真实当天日期并准确覆盖本次变更。
- [ ] 根 README、`bot/README.md`、`TODO.md` 与本次事实一致；运营文档和群 briefing 同步。
- [ ] `refs.json` JSON 格式有效；不提交密钥、webhook、PAT 或 state/cache 临时文件。
- [ ] 检查 `git diff`，确保没有将历史审计日志当作“修复”回写。

## 6. 发布后验收（四个独立面）

1. **GitHub Actions**：手动 Run workflow 或等待下一 ET 扫描窗口；分别确认 `build` / `deploy` 和 Lark 投递结果，不能只看 workflow 总体颜色。单纯 `git push` 不会立即刷新 Pages / 推送。
2. **Pages**：打开根地址，检查 `data.json` 生成时间与本次 Actions 一致，并确认 `changelog[0]` 等于本次 `CHANGELOG.md` 顶部条目后，才可称 Bot「最近更新」已刷新；资产覆盖应为现货 62 / 合约 39 / 共 81 / 监控 75，且仅从现货移除、后续以合约重上的标的应正确显示「仅合约」；`ticker_aliases` 应包含 `BBX → BB`。不要手工修改构建产物 `site_data.json`。
3. **定时推送**：下一次有内容的运行检查官方链接、第三方链接、预测/催办状态和 @ 名单；单源与合约门槛 review 应明确写「核验、勿执行」且不能触发正式催办 @；合约 ≤3% 应正常首报并写「本次无需操作」，但不得进入 30/14 日重复催办；15–29 天静默期不应收到只有统计没有明细的卡片。
4. **Railway Bot**：确认已部署到同一提交/镜像；在群里测试 `帮助`、`查 AAPL`、`查 BBX`（应返回 `BB`）、`查 BRK-B`、`查 SKHY`、`临近催办`、`观察预测`。Pages 更新不代表 Railway 已更新。

## 一句话流程

判定影响面 → 统一事件数据 → 自动检查 → 文档/CHANGELOG → 提交推送 → Run workflow → 同时验收 Actions、Pages、Lark 推送与 Railway Bot。
