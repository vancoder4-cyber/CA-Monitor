# 待办 / 后续跟进

## 数据质量:待人工确认的存量异常

线上目前挂着一批异常(冲突 / 空缺 / 单源未交叉验证),**零容忍不豁免,只有人工发『确认』才消解**。
已定性、可以直接确认的几类(核对后在群里发 `确认 代码 [正确值]`):

- [ ] **ADR 扣税差异**(TSM / ASML / NVO / NOK / BABA):Alpaca 报的是扣预扣税后的净额,**以宣告原值为准**
- [ ] **拆股回溯调整**(KLAC 等):yfinance 把历史分红按拆股比例除过,**以当时实际宣告值为准**
- [ ] **四舍五入**(WMT / HPE):yfinance 只保留 3 位小数,**以多源一致的精确值为准**
- [ ] **单源未交叉验证**(TSM 9/16、CRWD 4:1、KLAC 10:1 等):只有 1 个源报,核对后放行
- [ ] **未见宣告日的单源预估**(LLY 8/14、CAT 7/20、DELL 7/21、GLW 8/31):Nasdaq 无记录,大概率是 Alpaca 的预估 —— 确认公司是否真的公告了


> 不是 bug、但需要在某个时机回来处理的事项。完成后移到 CHANGELOG 并从这里删除。
> (群里大家提的功能需求在 `requests.md`;这里放的是内部技术跟进项。)

## FINX(TRKD-HS)接口稳定后复核 —— 约 2026-07 上旬

供方告知接口仍在调整、约 2 周(demo 阶段)。等稳定后回来做:

- [ ] **核对 RIC 映射**:`config.FINX_RIC` 里几个非 Nasdaq 标的(LLY/CRCL→`.N`、EWY→`.K` 等),用实际能取到数据验证;默认 `.O` 的也抽查。取不到数据的调整后缀。
- [ ] **复核历史空缺降噪**:目前 FINX 仅对近 `SHORT_HISTORY_GAP_DAYS`(45)天内及未来的事件参与空缺判定(见 `config.py` / `reconcile.py`)。接口补齐历史后,看是否可放宽或取消这个豁免,让 FINX 也纳入历史一致性核对。
- [ ] **字段再核对**:`sources.fetch_finx` 的字段是按当前 demo 返回写的(`dividendExDate/dividendRecordDate/dividendPaymentDate`、`splitAnnouncement` 等),已做防御式多名兼容;稳定后对照正式文档再确认一遍。
- [ ] **轮换 FINX 密码**:demo 密码曾在沟通中明文出现,确认已在 Railway + GitHub secret 两处换成新密码。
- [ ] **拆股/并购验证**:目前主要用 AAPL/MSFT 分红验证过;等有拆股或并购样本时,验证 `STOCK_SPLIT` / `MA` / `OTHER_CORPORATE_ACTION` 解析正确。
