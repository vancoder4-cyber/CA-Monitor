# -*- coding: utf-8 -*-
"""从 Pages 发布的 data.json 构建 Lark 交互卡片。"""
import datetime as dt
ETYPE_CN = {"dividend": "分红", "split": "拆股", "filing": "并购/公告"}


def _val(x):
    if x.get("amount") is not None:
        return f" ${x['amount']}"
    if x.get("ratio"):
        return f" {x['ratio']}"
    return ""


def _dates(x):
    s = f"除息 {x['date']}"
    if x.get("record"):
        s += f" · 登记 {x['record']}"
    if x.get("pay"):
        s += f" · 派发 {x['pay']}"
    return s


def _card(title, template, elements, site_url, btn_text):
    if site_url:
        elements = elements + [
            {"tag": "hr"},
            {"tag": "action", "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": btn_text},
                "url": site_url, "type": "primary"}]},
        ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
        "elements": elements,
    }


def calendar_card(data, site_url):
    pending = data.get("pending", [])
    gen = data.get("generated", "")
    if not pending:
        elems = [{"tag": "div", "text": {"tag": "lark_md", "content": "近期暂无已公告未执行的事件。"}}]
        return _card(f"📅 公司行动日历 · {gen}", "blue", elems, site_url, "打开网页日历")
    lines = []
    for x in pending[:30]:
        prod = ("[" + "+".join(x["products"]) + "] ") if x.get("products") else ""
        lines.append(f"• {prod}**{x['ticker']}** {ETYPE_CN.get(x['etype'], x['etype'])}{_val(x)} — "
                     f"<font color='red'>还剩 {x['days']} 天</font>\n　{_dates(x)}")
    elems = [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}]
    return _card(f"📅 公司行动日历 · {gen}", "blue", elems, site_url, "打开网页日历")


def alert_card(data, site_url):
    c = data.get("counts", {})
    gen = data.get("generated", "")
    template = "red" if (c.get("conflicts") or c.get("gaps")) else "blue"
    elems = [{"tag": "div", "text": {"tag": "lark_md",
              "content": f"📣 新公告 **{c.get('announced',0)}**　⏳ 待执行 **{c.get('pending',0)}**　🆕 新发现 **{c.get('new',0)}**"
                         f"　❗冲突 **{c.get('conflicts',0)}**　🕳 空缺 **{c.get('gaps',0)}**"}},
             {"tag": "hr"}]

    def sec(title, lines):
        if lines:
            elems.append({"tag": "div", "text": {"tag": "lark_md",
                         "content": f"**{title}**\n" + "\n".join(lines[:20])}})

    # 精简为「当日总览」:只给数据质量(冲突/空缺),明细交给专项指令
    conf = [f"• **{g['ticker']}** {ETYPE_CN.get(g['etype'],g['etype'])} {g['date']}: " + "; ".join(g.get("conflicts", []))
            for g in data.get("conflicts", [])]
    sec("❗ 字段冲突(零容忍)", conf)
    gap = [f"• **{g['ticker']}** {ETYPE_CN.get(g['etype'],g['etype'])} {g['date']}: " + "; ".join(g.get("gaps", []))
           for g in data.get("gaps", [])]
    sec("🕳 数据空缺", gap)

    elems.append({"tag": "div", "text": {"tag": "lark_md",
                  "content": "明细查询:**风险** / **今日** / **本周** / **新公告** / **覆盖**"}})
    return _card(f"🔔 当日总览 · {gen}", template, elems, site_url, "打开预警面板")


HELP_TEXT = (
    "可用指令(@我 + 关键词):\n"
    "• **关于** —— 这是什么、数据源、规则、更新时点\n"
    "• **风险** —— 当日风控清单(拆股/并购退市/冲突 + 风控动作)\n"
    "• **今日** / **本周** —— 今天要处理的 / 未来 7 天\n"
    "• **新公告** —— 最新刚宣告的事件\n"
    "• **覆盖** / **资产** —— 各标的在现货/合约的覆盖情况\n"
    "• **日历** —— 当月公司行动月历(图)\n"
    "• **预警** —— 当日总览(计数 + 冲突/空缺)+ 面板链接\n"
    "• **帮助** —— 显示本说明"
)


# ---------------- 关于 / 介绍 ----------------
def about_card(data, site_url):
    gen = data.get("generated", "")
    content = (
        "**CA问答助手** —— 公司行动(Corporate Actions)监控\n"
        "盯 **现货(24 支美股)+ 合约范围(22 个)**标的的:分红 / 拆股·合股 / 并购 / 分拆 / 退市·代码变更。"
        "合约里的 ETF(QQQ/EWY)监控分红;商品/海外(XAU/WTI/SKHYNIX 等)无公司行动,仅列入覆盖。\n\n"
        "**数据源(7,多源交叉核对·零容忍)**\n"
        "yfinance · FMP · Alpha Vantage · Nasdaq · Tiingo · Alpaca · SEC EDGAR\n\n"
        "**核对规则**:同一事件多源比对,字段(除息/登记/派发/金额/比例)不一致或某源缺失即告警;"
        "每事件标 已确认(≥2源一致)/ 单源待核实 / 有冲突;并标注 现货/合约 + 对应风控动作。\n\n"
        "**更新**:每交易日 3 次 —— 开盘后 9:35 / 盘中 12:45 / 收盘后 16:05(美东)。\n\n"
        "**提前预警(运营催办)**:以除息日为准,距 **30/14** 天提前知会;**7** 天开始准备文案并明确排期;"
        "**3** 天确保文案全部写完;**1** 天确认文案就绪并备好定时发送。每条标明现货/合约;临近时只推最接近的一轮(风控提醒待定)。\n\n"
        "**指令**(@我 + 关键词):关于 · 风险 · 今日 · 本周 · 新公告 · 日历 · 预警 · 帮助\n\n"
        f"_数据更新于 {gen}_"
    )
    return _card("ℹ️ 关于 CA问答助手", "blue",
                 [{"tag": "div", "text": {"tag": "lark_md", "content": content}}],
                 site_url, "打开网页面板")


def _line(e, with_days=True, with_risk=False):
    prod = ("[" + "+".join(e["products"]) + "] ") if e.get("products") else ""
    s = f"• {prod}**{e['ticker']}** {ETYPE_CN.get(e['etype'], e['etype'])}{_val(e)}"
    if e.get("etype") == "filing" and e.get("note"):
        s = f"• {prod}**{e['ticker']}** {e['note']}"
        if e.get("url"):
            s += f" [SEC原文]({e['url']})"
    if with_risk:
        for r in e.get("risk", []):
            s += f"\n　⚠️ {r}"
    return s


# ---------------- 风险(风控清单)----------------
def risk_card(data, site_url):
    today = dt.date.today().isoformat()
    lo30 = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    cal = data.get("calendar", [])
    splits = [e for e in cal if e["etype"] == "split" and (e.get("date") or "") >= today]
    structurals = [e for e in cal if e["etype"] == "filing" and (e.get("date") or "") >= lo30]
    conflicts = data.get("conflicts", [])
    n = len(splits) + len(structurals) + len(conflicts)
    template = "red" if n else "green"
    elems = [{"tag": "div", "text": {"tag": "lark_md",
              "content": f"当日风控总览 · 拆股 **{len(splits)}** · 并购/退市 **{len(structurals)}** · 数据冲突 **{len(conflicts)}**"}},
             {"tag": "hr"}]

    def sec(title, lines):
        if lines:
            elems.append({"tag": "div", "text": {"tag": "lark_md",
                         "content": f"**{title}**\n" + "\n".join(lines[:20])}})

    sec("✂️ 即将拆股/合股(调乘数·保证金·防穿仓)",
        [_line(e, with_risk=True) for e in splits])
    sec("🤝 并购 / 退市(评估暂停·移仓·强结)",
        [_line(e) for e in structurals])
    sec("❗ 数据冲突(动手前先核实)",
        [f"• **{g['ticker']}** {ETYPE_CN.get(g['etype'], g['etype'])} {g['date']}: "
         + "; ".join(g.get("conflicts", [])) for g in conflicts])
    if n == 0:
        elems.append({"tag": "div", "text": {"tag": "lark_md", "content": "✅ 当前无风控事项。"}})
    return _card("⚠️ 风控清单", template, elems, site_url, "打开预警面板")


# ---------------- 今日 / 本周 ----------------
def _window_card(data, site_url, days, title):
    today = dt.date.today()
    hi = (today + dt.timedelta(days=days)).isoformat()
    today_s = today.isoformat()
    cal = data.get("calendar", [])
    hits = []
    for e in cal:
        # 命中:除息/生效/公告(date)、登记、派发、宣告 任一落在窗口内
        keys = {"除息/生效": e.get("date"), "登记": e.get("record"),
                "派发": e.get("pay"), "宣告": e.get("decl")}
        for label, d in keys.items():
            if d and today_s <= d <= hi:
                hits.append((d, label, e))
    hits.sort(key=lambda x: x[0])
    if not hits:
        body = f"{title}暂无公司行动关键日。"
        return _card(f"🗓 {title}", "green",
                     [{"tag": "div", "text": {"tag": "lark_md", "content": body}}], site_url, "打开网页日历")
    lines = []
    for d, label, e in hits[:40]:
        prod = ("[" + "+".join(e["products"]) + "] ") if e.get("products") else ""
        tag = "" if days == 0 else f"{d} "
        lines.append(f"• {tag}{prod}**{e['ticker']}** {ETYPE_CN.get(e['etype'], e['etype'])}{_val(e)} —— **{label}日**")
    elems = [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}]
    return _card(f"🗓 {title}", "blue", elems, site_url, "打开网页日历")


def today_card(data, site_url):
    return _window_card(data, site_url, 0, "今日")


def week_card(data, site_url):
    return _window_card(data, site_url, 7, "本周(未来7天)")


def announce_card(data, site_url):
    ann = data.get("announced", [])
    if not ann:
        return _card("📣 新公告", "green",
                     [{"tag": "div", "text": {"tag": "lark_md", "content": "近期暂无新宣告事件。"}}],
                     site_url, "打开预警面板")
    lines = []
    for x in ann[:30]:
        prod = ("[" + "+".join(x["products"]) + "] ") if x.get("products") else ""
        d = f" · 还剩 {x['days']} 天" if x.get("days") is not None else ""
        lines.append(f"• {prod}**{x['ticker']}** {ETYPE_CN.get(x['etype'], x['etype'])}{_val(x)} —— "
                     f"宣告 {x.get('decl')} · 除息 {x['date']}{d}")
    return _card("📣 新公告(刚宣告)", "blue",
                 [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}],
                 site_url, "打开预警面板")


def coverage_card(data, site_url):
    cov = data.get("coverage", [])
    if not cov:
        return _card("📋 资产覆盖", "blue",
                     [{"tag": "div", "text": {"tag": "lark_md", "content": "暂无覆盖数据。"}}],
                     site_url, "打开网页面板")
    n = len(cov)
    n_spot = sum(1 for c in cov if c["spot"])
    n_contract = sum(1 for c in cov if c["contract"])
    n_mon = sum(1 for c in cov if c["monitored"])
    both = [c["ticker"] for c in cov if c["spot"] and c["contract"]]
    spot_only = [c["ticker"] for c in cov if c["spot"] and not c["contract"]]
    contract_only = [c["ticker"] for c in cov if c["contract"] and not c["spot"]]
    na = [f"{c['ticker']}({c['type_cn']})" for c in cov if not c["monitored"]]
    content = (
        f"现货 **{n_spot}** · 合约 **{n_contract}** · 共 **{n}** 个资产(监控 {n_mon} · 不适用 {n - n_mon})\n\n"
        f"**现货+合约**:{'、'.join(both) or '—'}\n\n"
        f"**仅现货**:{'、'.join(spot_only) or '—'}\n\n"
        f"**仅合约**:{'、'.join(contract_only) or '—'}\n\n"
        f"**不适用**(商品/海外,无公司行动):{'、'.join(na) or '—'}"
    )
    return _card("📋 资产覆盖(现货/合约)", "blue",
                 [{"tag": "div", "text": {"tag": "lark_md", "content": content}}],
                 site_url, "打开网页面板")
