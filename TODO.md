# 待办 / 后续跟进

> 不是 bug、但需要在某个时机回来处理的事项。完成后移到 CHANGELOG 并从这里删除。
> (群里大家提的功能需求在 `requests.md`;这里放的是内部技术跟进项。)

## FINX(TRKD-HS)接口稳定后复核 —— 约 2026-07 上旬

供方告知接口仍在调整、约 2 周(demo 阶段)。等稳定后回来做:

- [ ] **核对 RIC 映射**:`config.FINX_RIC` 里几个非 Nasdaq 标的(LLY/CRCL→`.N`、EWY→`.K` 等),用实际能取到数据验证;默认 `.O` 的也抽查。取不到数据的调整后缀。
- [ ] **复核历史空缺降噪**:目前 FINX 仅对近 `SHORT_HISTORY_GAP_DAYS`(45)天内及未来的事件参与空缺判定(见 `config.py` / `reconcile.py`)。接口补齐历史后,看是否可放宽或取消这个豁免,让 FINX 也纳入历史一致性核对。
- [ ] **字段再核对**:`sources.fetch_finx` 的字段是按当前 demo 返回写的(`dividendExDate/dividendRecordDate/dividendPaymentDate`、`splitAnnouncement` 等),已做防御式多名兼容;稳定后对照正式文档再确认一遍。
- [ ] **轮换 FINX 密码**:demo 密码曾在沟通中明文出现,确认已在 Railway + GitHub secret 两处换成新密码。
- [ ] **拆股/并购验证**:目前主要用 AAPL/MSFT 分红验证过;等有拆股或并购样本时,验证 `STOCK_SPLIT` / `MA` / `OTHER_CORPORATE_ACTION` 解析正确。
