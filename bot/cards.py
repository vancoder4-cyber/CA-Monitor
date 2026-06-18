# -*- coding: utf-8 -*-
"""从 Pages 发布的 data.json 构建 Lark 交互卡片。"""
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
              "content": f"⏳ 待执行 **{c.get('pending',0)}**　🆕 新发现 **{c.get('new',0)}**"
                         f"　❗冲突 **{c.get('conflicts',0)}**　🕳 空缺 **{c.get('gaps',0)}**"}},
             {"tag": "hr"}]

    def sec(title, lines):
        if lines:
            elems.append({"tag": "div", "text": {"tag": "lark_md",
                         "content": f"**{title}**\n" + "\n".join(lines[:20])}})

    pend = []
    for x in data.get("pending", []):
        prod = ("[" + "+".join(x["products"]) + "] ") if x.get("products") else ""
        line = f"• {prod}**{x['ticker']}** {ETYPE_CN.get(x['etype'],x['etype'])}{_val(x)} — <font color='red'>还剩 {x['days']} 天</font>"
        for r in x.get("risk", []):
            line += f"\n　⚠️ {r}"
        pend.append(line)
    sec("⏳ 待执行(已公告未发生)", pend)

    conf = [f"• **{g['ticker']}** {ETYPE_CN.get(g['etype'],g['etype'])} {g['date']}: " + "; ".join(g.get("conflicts", []))
            for g in data.get("conflicts", [])]
    sec("❗ 字段冲突(零容忍)", conf)
    gap = [f"• **{g['ticker']}** {ETYPE_CN.get(g['etype'],g['etype'])} {g['date']}: " + "; ".join(g.get("gaps", []))
           for g in data.get("gaps", [])]
    sec("🕳 数据空缺", gap)
    new = []
    for g in data.get("new", []):
        s = f"• **{g['ticker']}** {ETYPE_CN.get(g['etype'],g['etype'])} {g['date']} {g.get('note','')}"
        if g.get("sec_url"):
            s += f" [SEC原文]({g['sec_url']})"
        new.append(s)
    sec("🆕 新发现事件", new)

    return _card(f"🔔 公司行动预警 · {gen}", template, elems, site_url, "打开预警面板")


HELP_TEXT = (
    "可用指令(@我 + 关键词):\n"
    "• **日历** —— 近期公司行动日历卡片 + 网页截图\n"
    "• **预警** / **面板** —— 当前待执行/冲突/空缺摘要 + 面板链接\n"
    "• **帮助** —— 显示本说明"
)
