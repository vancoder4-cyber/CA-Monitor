# -*- coding: utf-8 -*-
"""发布前回归：官方正式化 + 分红引用契约必须同时覆盖网页、推送和交互 Bot。

不用网络、不改 state；CI 在安装依赖后执行。这个 fixture 专门防止重现：
「单标的卡只有 Nasdaq、推送和网页又是另一条链接」以及「官方已宣告仍被当预测」两类漂移。
"""
import json
import os
import sys
import inspect

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bot"))

import reconcile as R  # noqa: E402
import run  # noqa: E402
import report  # noqa: E402
import notify_lark  # noqa: E402
import cards  # noqa: E402
import ack  # noqa: E402


VISA_URL = "https://investor.visa.com/stock-information/dividends/default.aspx?LanguageId=1"
STOCKANALYSIS_URL = "https://stockanalysis.com/stocks/v/dividend/"


def must(condition, message):
    if not condition:
        raise AssertionError(message)


def text_of(card):
    return json.dumps(card, ensure_ascii=False)


def main():
    refs = run.load_refs()
    override = (refs.get("official_event_overrides") or {}).get("V|dividend|2026-08-11") or {}
    must(refs.get("ir_dividend", {}).get("V") == VISA_URL, "V 缺少 canonical Visa IR 分红页")
    must(override.get("url") == VISA_URL, "V 缺少已核验官方事件覆盖")
    must(override.get("declaration_date") == "2026-07-28", "V 官方宣告日未登记")
    must(ack.authoritative_source("V", "dividend", refs.get("ir_dividend")) == VISA_URL,
         "审计写回不会从 Pages refs 使用 Visa 官方链接")
    must("refs_ir" in inspect.signature(ack.add_ack).parameters
         and "refs_ir" in inspect.signature(ack.add_forecast).parameters,
         "确认/观察留痕缺少 Pages refs 注入参数")

    g = R.EventGroup(
        ticker="V", etype="dividend", anchor_date="2026-08-11",
        by_source={"Alpaca": {"ex_date": "2026-08-11", "record_date": "2026-08-11",
                               "pay_date": "2026-09-01", "amount": 0.67}},
        sources_ok=["Alpaca"],
    )
    R.evaluate_group(g)
    groups = {"V": [g]}
    run.apply_official_event_overrides(groups, refs)
    must("CompanyIR" in g.by_source, "官方覆盖层没有合入事件组")
    must(R.pick_value(g.by_source, "declaration_date") == "2026-07-28", "官方宣告日没有成为统一取值")
    must(g.status == "confirmed" and not R.is_disputed(g), "官方一致事件没有转为正式")

    # 生产缓存可能暂时没有供应商条目；仅有人工逐项核验的 CompanyIR 时也必须
    # 是正式事件，不能在网页/日历中被渲染成“单源待核实”。
    official_only = R.EventGroup(ticker="V", etype="dividend", anchor_date="2026-08-11")
    run.apply_official_event_overrides({"V": [official_only]}, refs)
    must(official_only.status == "confirmed" and R.has_official_source(official_only.by_source),
         "单独的 CompanyIR 官方覆盖仍被误标为单源")

    run.attach_event_references(g, refs, {})
    links = getattr(g, "references", [])
    must(any(x.get("url") == VISA_URL and x.get("kind") == "official_event" for x in links),
         "官方本次公告链接未下发")
    must(any(x.get("url") == STOCKANALYSIS_URL and x.get("kind") == "third_party" for x in links),
         "第三方交叉核对链接未下发")

    event = {
        "ticker": "V", "etype": "dividend", "date": "2026-08-11", "decl": "2026-07-28",
        "record": "2026-08-11", "pay": "2026-09-01", "amount": 0.67, "ratio": None,
        "days": 12, "round": 12, "status": "confirmed", "amt_srcs": 1, "official": True,
        "acked": False, "srcs": ["Alpaca", "CompanyIR"], "products": ["现货"],
        "risk": ["现货:除息日成本基准调整,持仓与对账核对"], "ops": "进入 14 天窗口:每日跟进",
        "risk_copy": "风控提醒:待风控团队明确(占位)", "first": "2026-07-28",
    }
    run.attach_event_references(event, refs, {})
    must(event.get("primary_url") == VISA_URL, "统一主链接不是 Visa 官方页")
    must(event.get("third_party_url") == STOCKANALYSIS_URL, "统一第三方链接不正确")
    must(event.get("decl_url") == "" and event.get("ir_url") == VISA_URL,
         "旧版 Bot 的兼容字段把官方 IR 错标为宣告 8-K")

    # 有真实 SEC 8-K 时，旧 Bot 的「宣告 8-K」标签必须指向它；没有时才退到 IR。
    fixture_filing = "https://www.sec.gov/Archives/edgar/data/1403161/fixture-v-dividend.htm"
    legacy_event = dict(event)
    run.attach_event_references(legacy_event, refs, {"V": [("2026-07-28", fixture_filing, "8.01")]})
    must(legacy_event.get("decl_url") == fixture_filing and legacy_event.get("ir_url") == VISA_URL,
         "旧版 Bot 的 filing / IR 兼容字段不准确")
    must("单源未交叉验证" not in cards._val(event), "官方已核验事件仍被金额门禁拦截")

    data = {
        "generated": "fixture", "refs": refs.get("ir_dividend", {}),
        "coverage": [{"ticker": "V", "name": "Visa", "spot": True, "contract": False,
                      "type_cn": "个股", "monitored": True}],
        "pending": [event], "calendar": [event], "recent_declares": [event], "announced": [event],
        "forecasts": [], "forecast_updates": [], "conflicts": [], "gaps": [],
    }
    for name, card in {
        "lookup": cards.lookup_card(data, "V", ""),
        "upcoming": cards.upcoming_card(data, ""),
        "calendar": cards.calendar_card(data, ""),
        "announce": cards.announce_card(data, ""),
    }.items():
        rendered = text_of(card)
        must(VISA_URL in rendered and STOCKANALYSIS_URL in rendered,
             f"交互 Bot {name} 未同时渲染官方 + 第三方链接")
        must("nasdaq.com/market-activity/stocks/v/dividend-history" not in rendered,
             f"交互 Bot {name} 仍回退为 Nasdaq")

    alerts = {
        "new": [], "rounds": [event], "conflicts": [], "gaps": [], "pending": [event],
        "announced": [event], "resolved": [], "forecasts": [], "forecast_updates": [],
        "review": {},
    }
    pushed = text_of(notify_lark._build_card(alerts, {"generated": "fixture"}))
    must(VISA_URL in pushed and STOCKANALYSIS_URL in pushed, "定时推送未使用统一双链接")

    digest = report.build_text_digest(
        {**alerts, "forecast_updates": [{**event, "kind": "declared"}]},
        {"generated": "fixture"},
    )
    must("公司官方宣告" in digest and "预测失效" not in digest,
         "官方正式化在文本 digest 中被误写为预测失效")

    page = report.build_site(groups, {"V": {"Alpaca": "ok"}}, alerts, {"generated": "fixture"})
    must(VISA_URL in page and STOCKANALYSIS_URL in page, "网页未使用统一双链接")
    must("nasdaq.com/market-activity/stocks/v/dividend-history" not in page, "网页仍回退为 Nasdaq")

    print("✅ 公司行动展示面一致性检查通过：官方正式化、Bot、推送、网页、月历链接均已覆盖。")


if __name__ == "__main__":
    main()
