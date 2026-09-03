# 待办 / 后续跟进

> 这里只放仍然有效的技术或运营跟进，不保存某一次扫描的静态异常清单。实时冲突、空缺和预测观察以 Pages `data.json` / 群内卡片为准；解决后移入 `CHANGELOG.md`。

## 发布闭环

- [ ] **Railway Bot 版本可见性**：下一次 Bot 发版时，在 Railway 增加可见的 build/commit 标识，或在 `关于`卡片显示当前 Bot build。验收必须同时检查 Actions、Pages 和 Railway，而不是只看其中一个。
- [ ] **自动化生产 smoke**：在不发送真实群消息的前提下，增加可选 smoke，读取生产 `data.json` 并验证当前范围为现货 62 / 合约 39 / 覆盖 81 / 监控 75、`SKHY` 与 Bot aliases 已发布、仅从现货移除后以合约重上的标的显示正确；历史 Visa fixture 仅在本地引用契约测试中保留。

## 官方事件与核对来源

- [ ] **补齐高优先级标的的官方 IR**：有明确分红政策或 ADR 毛额口径的标的，先填 `refs.json.ir_dividend`；没有可靠链接就保留为空，由系统回退 SEC 公司备案 + 第三方交叉核对，不能伪造或使用滞后的 Nasdaq 页面。
- [ ] **逐项覆盖层治理**：`official_event_overrides` 只收录人工打开并确认过的具体公司事件。源端补齐后继续保留为可追溯记录；若与自动源不一致，优先调查冲突，不能删除覆盖层来消警。
- [ ] **ADR 毛额**：对仍在范围内、未来会派息的海外直接上市标的，确认公司公告本币毛额与存托凭证 USD 的差异，避免将预扣税后净额写成最终值。

## FINX（TRKD-HS）接口稳定后

- [ ] 抽查 `config.FINX_RIC`（NYSE / ETF 等非 `.O` 后缀）和 dividend / split / corporate-action 字段，按正式文档修正解析。
- [ ] 重新评估 `SHORT_HISTORY_SOURCES` / `SHORT_HISTORY_GAP_DAYS`：若 FINX 已补齐历史覆盖，再将其纳入更长历史的空缺检测。
- [ ] 确认 Railway 与 GitHub Secrets 中的 FINX 凭证已轮换，且不出现在日志、文档或提交中。
