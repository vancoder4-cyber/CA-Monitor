# -*- coding: utf-8 -*-
"""从 Pages 发布的 data.json 构建 Lark 交互卡片。"""
import datetime as dt
# 注意:不要在模块顶层 import ack —— ack 依赖 requests,而 CI 的「指令一致性检查」在装依赖**之前**
# 就 import cards,顶层拉 ack 会 ModuleNotFoundError。ack 只在 _authoritative_link 的兜底分支惰性导入。
ETYPE_CN = {"dividend": "分红", "split": "拆股", "filing": "并购/公告"}
# 异常/确认里带的那个日期,到底是哪个关键日:分红=除息日,拆股=生效日,filing=事件日
DATE_LABEL = {"dividend": "除息日", "split": "生效日", "filing": "事件日"}


def date_label(etype):
    return DATE_LABEL.get(etype, "关键日")


def _alert_copy(days):
    """催办文案(与 config.alert_copy 同口径,内联在 bot 侧,避免 import config —— bot 跑在 bot/ 目录,
    config.py 在仓库根不在其 import 路径)。改文案时两处保持一致。"""
    if days is None:
        return ""
    if days <= 1:
        return "⏱ 最后确认:仅剩 1 天 —— 确保文案已就绪、定时发送已备好。"
    if days <= 3:
        return f"⏱ 收尾:剩 {days} 天 —— 确保相关文案全部写完。"
    if days <= 7:
        return f"⏱ 催办:剩 {days} 天 —— 准备文案、明确「具体哪天」执行各项操作、完成排期。"
    if days <= 14:
        return f"进入 14 天窗口:剩 {days} 天 —— 每天跟进,确认本次活动安排。"
    return f"提前知会:距除息约 {days} 天 —— 请留意并排入计划(之后 14 天内会每天催)。"


def _authoritative_link(g, refs=None):
    """冲突核对来源。置信度分级:1 公司IR → 2 具体SEC filing → 3 聚合页。
    **要解决冲突时给两个源**:先最权威(T1/T2),再附聚合页快速核对 ——
    尤其 ADR,权威源是本币公告(NT$/DKK),聚合页补上 USD 数值,两边交叉核对。
    refs 传 data.json 的 refs(IR 映射),避免依赖机器人本地能否读到 refs.json。"""
    import ack  # 惰性导入(ack 依赖 requests);CI 的一致性检查在装依赖前 import cards,故不放模块顶层
    tk, et = g.get("ticker", ""), g.get("etype")
    url, label, tier = ack.verify_link(tk, et, g.get("src_url") or g.get("sec_url"), refs_ir=refs)
    if tier <= 2:   # 有权威源:权威 + 聚合 两个都给
        return f"　🔗 [{label}]({url}) · [聚合快速核对]({ack.quick_look(tk, et)})"
    return f"　🔗 [{label}]({url})"   # 本就只有 T3,不重复

# ===== 指令唯一来源(改指令只改这里;HELP_TEXT / 关于卡片 / parse_command 都由它生成)=====
# 顺序即匹配优先级。key 必须在 bot.py 的 on_message 里有对应 dispatch 分支。
COMMANDS = [
    # —— 上手/元信息 ——
    {"key": "about",    "kw": ["关于", "介绍", "about"],                 "name": "关于",   "desc": "这是什么、数据源、规则、更新时点"},
    {"key": "help",     "kw": ["帮助", "help"],                          "name": "帮助",   "desc": "显示指令说明"},
    {"key": "changelog","kw": ["最近更新", "更新日志", "更新", "changelog", "版本"], "name": "最近更新", "desc": "最近 3 次版本更新(更多见网页)"},
    # —— 按紧迫度:高 → 低 ——
    {"key": "risk",     "kw": ["风险", "风控", "risk"],                  "name": "风险",   "desc": "当日风控清单(拆股/并购退市/冲突 + 风控动作)"},
    {"key": "today",    "kw": ["今日", "今天", "today"],                 "name": "今日",   "desc": "T0 前后24小时的关键日(除息/登记/派发/宣告)"},
    {"key": "announce", "kw": ["新公告", "公告", "announce"],            "name": "新公告", "desc": "最近 5 个宣告的事件(已派发完标『已结束』)"},
    {"key": "week",     "kw": ["本周", "week"],                          "name": "本周",   "desc": "未来 7 天的公司行动"},
    {"key": "upcoming", "kw": ["临近催办", "催办", "临近", "待执行"],      "name": "临近催办", "desc": "已公告未发生的公司行动,按距除息天数排+催办文案(随时拉,不用等推送)"},
    {"key": "calendar", "kw": ["日历", "calendar", "cal"],              "name": "日历",   "desc": "当月公司行动月历(图)"},
    {"key": "coverage", "kw": ["覆盖", "资产", "标的", "coverage"],      "name": "覆盖",   "desc": "各标的在现货/合约的覆盖情况"},
    {"key": "lookup",   "kw": ["查代码", "查询", "代码", "查", "ticker", "lookup"], "name": "查代码", "desc": "@我 + 代码(如 AVGO)弹出该标的公司行动;只发『查代码』看用法"},
    {"key": "confirm",  "kw": ["确认", "confirm", "已核对"],              "name": "确认",   "desc": "人工放行异常:确认 CODE [正确值] [日期] [备注] —— 解除金额门禁、停报警、按你给的值显示并留痕"},
    {"key": "audit",    "kw": ["留痕", "审计", "audit", "log"], "name": "留痕",   "desc": "调取确认留痕:谁在何时确认了什么(可加代码只看某标的);要 Excel 用 tools/export_ack_log.py"},
    {"key": "request",  "kw": ["需求", "提报", "反馈", "建议", "feature"], "name": "需求提报", "desc": "提需求:需求 你的想法 —— 汇总给负责人用于迭代"},
]
# 注:顺序即匹配优先级 + 展示顺序。帮助不含 "?"(无匹配时默认即回帮助),避免「…?」误判。

def parse_command(text):
    """按 COMMANDS 顺序匹配关键词,返回 key;无匹配返回 help。bot.py 复用此函数。"""
    import re
    t = re.sub(r"@_user_\d+|@_all", "", text or "").strip().lower()
    for c in COMMANDS:
        if any(k.lower() in t for k in c["kw"]):
            return c["key"]
    return "help"


def _val(x):
    """金额/比例门禁:有未确认冲突 → 不给确定值,标『待人工确认·勿据此执行』。
    人工发「确认 代码 值」消解冲突后,才恢复显示确定值。"""
    if not x.get("acked") and not x.get("disputed") and (x.get("amt_srcs") or 0) == 1 and (
            x.get("amount") is not None or x.get("ratio")):
        v = x.get("amount") if x.get("amount") is not None else x.get("ratio")
        return f" <font color='orange'>⚠️单源未交叉验证({v})· 待人工确认,勿据此执行</font>"
    if x.get("disputed") and not x.get("acked"):
        vals = x.get("dispute_vals") or {}
        pairs = " / ".join(str(v) for v in dict.fromkeys(vals.values()))
        return f" <font color='red'>⚠️各源不一致({pairs})· 待人工确认,勿据此执行</font>"
    if x.get("amount") is not None:
        return f" ${x['amount']}"
    if x.get("ratio"):
        return f" {x['ratio']}"
    return ""


def _dates(x):
    """关键日链:宣告 · 登记 · 除息/生效 · 派发(有哪个显示哪个,与查代码口径一致)。"""
    lab = "除息" if x.get("etype") == "dividend" else "生效"
    parts = []
    if x.get("decl"):
        parts.append(f"宣告 {x['decl']}")
    if x.get("record"):
        parts.append(f"登记 {x['record']}")
    if x.get("date"):
        parts.append(f"{lab} {x['date']}")
    if x.get("pay"):
        parts.append(f"派发 {x['pay']}")
    return " · ".join(parts)


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


# 由 COMMANDS 自动生成(勿手改)
HELP_TEXT = "可用指令(@我 + 关键词):\n" + "\n".join(
    f"• **{c['name']}** —— {c['desc']}" for c in COMMANDS)

# 关于卡片里的指令名清单(由 COMMANDS 生成)
COMMAND_NAMES = " · ".join(c["name"] for c in COMMANDS)


# ---------------- 关于 / 介绍 ----------------
def about_card(data, site_url):
    gen = data.get("generated", "")
    content = (
        "**CA问答助手** —— 公司行动(Corporate Actions)监控\n"
        "盯 **现货(85 支美股)+ 合约范围(22 个)**标的的:分红 / 拆股·合股 / 并购 / 分拆 / 退市·代码变更。"
        "合约里的 ETF(QQQ/EWY/DRAM)监控分红;商品/海外(XAU/WTI/SKHYNIX 等)无公司行动,仅列入覆盖。\n\n"
        "**数据源(8 源,多源交叉核对·零容忍)**\n"
        "yfinance · FMP · Alpha Vantage · Nasdaq · Tiingo · Alpaca · SEC EDGAR · FINX(TRKD-HS)\n\n"
        "**关键日**:每条事件展示 **宣告 · 登记 · 除息/生效 · 派发**(缺哪个不显示哪个)。\n\n"
        "**取值规则**:金额/比例取 **多数票 + 源优先级**(要的是公司宣告的原值)。"
        "注意各源口径不同 —— yfinance 会按拆股回溯调整历史分红、还四舍五入;Alpaca 对 ADR 报的是扣预扣税后的净额。\n\n"
        "**🚦 金额门禁(重要)**:只有**多源交叉验证过且无冲突**的金额才显示确定值。否则一律封锁:\n"
        "• 各源不一致 → `⚠️各源不一致(a / b)· 待人工确认,勿据此执行`\n"
        "• 只有 1 个源报 → `⚠️单源未交叉验证(x)· 待人工确认,勿据此执行`\n"
        "**没确认过的数字,不要拿去执行。**\n\n"
        "**🙋 人工介入闭环(零容忍·不豁免)**:字段冲突 / 数据空缺 / 未见宣告日的单源预估,"
        "**每次扫描都重报、一直挂着**,并显示「已挂 N 天」;超 3 天没人确认会在推送里 **@ 负责人**。"
        "唯一消解方式:群里发 **确认 代码 [正确值] [日期] [备注]**(如 `确认 TSM 1.11362`;同一标的多条不同值时带日期,如 `确认 KLAC 2.3 2026-05-18`)"
        "—— 确认后门禁解除、按你给的值显示。每次确认**只追加、不删**地写入留痕库(谁/何时/改值前后/核对来源/备注),"
        "群里发 **留痕** 可随时调取,离线表用 `tools/export_ack_log.py` 导 Excel。\n\n"
        "**核对链接**:每条事件附原始出处(并购/退市→SEC 原文;分红→宣告 8-K / 公司 IR / Nasdaq),方便你核完再确认。\n\n"
        "**更新**:每交易日 3 次 —— 开盘后 9:35 / 盘中 12:45 / 收盘后 16:05(美东)。\n\n"
        "**提前预警(运营催办)**:以除息日为准,距 **30/14** 天提前知会;**7** 天开始准备文案并明确排期;"
        "**3** 天确保文案全部写完;**1** 天确认文案就绪并备好定时发送。每条标明现货/合约;临近时只推最接近的一轮(风控提醒待定)。\n\n"
        f"**指令**(@我 + 关键词):{COMMAND_NAMES}\n\n"
        f"_数据更新于 {gen}_"
    )
    return _card("ℹ️ 关于 CA问答助手", "blue",
                 [{"tag": "div", "text": {"tag": "lark_md", "content": content}}],
                 site_url, "打开网页面板")


def _line(e, with_days=True, with_risk=False):
    prod = ("[" + "+".join(e["products"]) + "] ") if e.get("products") else ""
    d = e.get("date") or ""
    if e.get("etype") == "filing" and e.get("note"):
        # filing 的 date = SEC 申报日(非执行日),显式标出以免「无日期」误以为紧急
        datestr = f" · 申报 {d}" if d else ""
        s = f"• {prod}**{e['ticker']}** {e['note']}{datestr}"
        if e.get("url"):
            s += f" [SEC原文]({e['url']})"
    else:
        label = "除息" if e.get("etype") == "dividend" else "生效"
        datestr = f" · {label} {d}" if d else ""
        s = f"• {prod}**{e['ticker']}** {ETYPE_CN.get(e['etype'], e['etype'])}{_val(e)}{datestr}"
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
    _refs = data.get("refs", {})

    def _conf_line(g):
        s = (f"• **{g['ticker']}** {ETYPE_CN.get(g['etype'], g['etype'])} {date_label(g['etype'])} {g['date']}: "
             + "; ".join(g.get("conflicts", [])))
        if g.get("adr_note"):   # ADR 预扣税提示:保证认税前毛额
            s += f"\n　<font color='red'>{g['adr_note']}</font>"
        return s + "\n" + _authoritative_link(g, _refs)

    sec("❗ 数据冲突(动手前先核实;先看权威源,再用聚合页交叉核对)",
        [_conf_line(g) for g in conflicts])
    if n == 0:
        elems.append({"tag": "div", "text": {"tag": "lark_md", "content": "✅ 当前无风控事项。"}})
    return _card("⚠️ 风控清单", template, elems, site_url, "打开预警面板")


# ---------------- 今日 / 本周 ----------------
def _window_card(data, site_url, lo_days, hi_days, title):
    today = dt.date.today()
    lo = (today + dt.timedelta(days=lo_days)).isoformat()
    hi = (today + dt.timedelta(days=hi_days)).isoformat()
    cal = data.get("calendar", [])
    hits = []
    for e in cal:
        # 命中:除息/生效/公告(date)、登记、派发、宣告 任一落在 [lo, hi] 窗口内
        keys = {"除息/生效": e.get("date"), "登记": e.get("record"),
                "派发": e.get("pay"), "宣告": e.get("decl")}
        for label, d in keys.items():
            if d and lo <= d <= hi:
                hits.append((d, label, e))
    hits.sort(key=lambda x: x[0])
    if not hits:
        body = f"{title}暂无公司行动关键日。"
        return _card(f"🗓 {title}", "green",
                     [{"tag": "div", "text": {"tag": "lark_md", "content": body}}], site_url, "打开网页日历")
    lines = []
    for d, label, e in hits[:40]:
        prod = ("[" + "+".join(e["products"]) + "] ") if e.get("products") else ""
        flag = "🔴 今天 " if d == today.isoformat() else ""
        lines.append(f"• {flag}{d} {prod}**{e['ticker']}** {ETYPE_CN.get(e['etype'], e['etype'])}{_val(e)} —— **{label}日**")
    elems = [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}]
    return _card(f"🗓 {title}", "blue", elems, site_url, "打开网页日历")


def today_card(data, site_url):
    # T0 ±24 小时:昨天/今天/明天 的关键日
    return _window_card(data, site_url, -1, 1, "今日(前后24小时)")


def week_card(data, site_url):
    return _window_card(data, site_url, 0, 7, "本周(未来7天)")


def upcoming_card(data, site_url):
    """临近催办:已公告未发生的事件,按距除息天数排 + 催办文案(与推送同口径,随时可拉)。"""
    import ack  # bot/ 内模块,可安全导入(核对来源解析)
    gen = data.get("generated", "")
    refs_ir = data.get("refs", {})    # IR 映射从 data.json 读,避免依赖机器人本地 refs.json
    pend = sorted(data.get("pending", []), key=lambda x: x.get("days", 9999))
    if not pend:
        elems = [{"tag": "div", "text": {"tag": "lark_md", "content": "近期暂无已公告未执行的公司行动。"}}]
        return _card(f"🔔 临近催办 · {gen}", "blue", elems, site_url, "打开网页面板")
    lines = []
    for x in pend[:30]:
        prod = ("[" + "+".join(x["products"]) + "] ") if x.get("products") else ""
        line = (f"• {prod}**{x['ticker']}** {ETYPE_CN.get(x['etype'], x['etype'])}{_val(x)} — "
                f"<font color='red'>还剩 {x['days']} 天</font>\n　{_dates(x)}")
        srcs = x.get("srcs") or []
        if srcs:
            n = len(srcs)
            tag = "单源" if n == 1 else f"{n}源"
            line += f"\n　📡 数据源({tag}):{', '.join(srcs)}"
        if x.get("days") is not None:
            line += f"\n　👉 {_alert_copy(x['days'])}"
        if not x.get("confirmed", True):
            line += ("\n　⚠️ <font color='orange'>未见宣告日/单源(可能是预估,公司尚未正式公告)—— "
                     "核对后发『确认 代码 值 日期』放行</font>")
        # 两个核对入口:第一方(src_url 具体 filing → 公司 IR → SEC 备案)+ 第三方聚合页
        import ack  # bot/ 内模块,可安全导入
        first = x.get("src_url") or ack.authoritative_source(x["ticker"], x.get("etype"), refs_ir)
        line += (f"\n　🔗 核对:[公司filing/SEC]({first}) · "
                 f"[第三方数据]({ack.quick_look(x['ticker'], x.get('etype'))})")
        lines.append(line)
    elems = [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}]
    return _card(f"🔔 临近催办(≤14天每天 · 30天知会)· {gen}", "blue", elems, site_url, "打开网页面板")


def announce_card(data, site_url):
    # 最近 5 个被宣告(declaration date)的事件;已派发完的标「已结束」
    ann = data.get("recent_declares") or data.get("announced", [])
    if not ann:
        return _card("📣 新公告", "green",
                     [{"tag": "div", "text": {"tag": "lark_md", "content": "近期暂无宣告事件。"}}],
                     site_url, "打开网页面板")
    lines = []
    for x in ann[:5]:
        prod = ("[" + "+".join(x["products"]) + "] ") if x.get("products") else ""
        if x.get("ended"):
            status = " · ✅ 已结束"
        elif x.get("days") is not None and x["days"] >= 0:
            status = f" · 还剩 {x['days']} 天"
        else:
            status = ""
        lines.append(f"• {prod}**{x['ticker']}** {ETYPE_CN.get(x['etype'], x['etype'])}{_val(x)} —— "
                     f"宣告 {x.get('decl')} · 除息 {x['date']}{status}")
    return _card("📣 新公告(最近 5 个宣告)", "blue",
                 [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}],
                 site_url, "打开网页面板")


def changelog_card(data, site_url):
    chg = data.get("changelog", [])
    if not chg:
        return _card("🆕 最近更新", "blue",
                     [{"tag": "div", "text": {"tag": "lark_md", "content": "暂无更新记录。"}}],
                     site_url, "打开网页面板")
    parts = []
    for e in chg[:3]:
        items = "\n".join(f"　• {i}" for i in e["items"][:6])
        parts.append(f"**{e['head']}**\n{items}")
    content = "\n\n".join(parts)
    if len(chg) > 3:
        content += f"\n\n…… 共 {len(chg)} 次更新,更多见网页"
    log_url = (site_url + "?tab=log") if site_url else site_url   # 直接跳网页「更新日志」标签页
    return _card("🆕 最近更新", "blue",
                 [{"tag": "div", "text": {"tag": "lark_md", "content": content}}],
                 log_url, "查看完整更新日志")


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


# ---------------- 查代码(单标的)----------------
def find_ticker(text, data):
    """从消息里抽出一个『已覆盖』的标的代码(忽略 @、指令词)。无则 None。"""
    import re
    known = {c["ticker"] for c in data.get("coverage", [])}
    toks = re.findall(r"[A-Za-z]{1,6}", (text or "").upper())
    for t in toks:
        if t in known:
            return t
    return None


def _sec_company_url(ticker):
    # EDGAR 的 CIK 参数可直接用代码解析到公司,列出该标的全部备案
    return ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
            f"&CIK={ticker}&type=&dateb=&owner=include&count=40")


def _days_str(d):
    if not d:
        return ""
    try:
        n = (dt.date.fromisoformat(d) - dt.date.today()).days
    except Exception:
        return d
    rel = "今天" if n == 0 else (f"{n}天后" if n > 0 else f"{-n}天前")
    return f"{d}({rel})"


def _ops_hint(days):
    """按距除息/生效天数给运营公告处理提醒(与预警节奏 30/14/7/3/1 一致)。"""
    if days is None or days < 0:
        return ""
    if days <= 1:
        return "运营:最后确认 —— 公告文案就绪、定时发送已设置"
    if days <= 3:
        return "运营·催办:确保公告文案全部写完"
    if days <= 7:
        return "运营·催办:开始准备公告文案,明确各项执行的具体日期/排期"
    if days <= 14:
        return "运营:提前知会,确认本次活动安排"
    if days <= 30:
        return "运营:提前知会,留意并排入计划"
    return ""


def lookup_card(data, ticker, site_url):
    if not ticker:
        content = (
            "**用法:@CA问答助手 + 空格 + 标的代码**,即可弹出该标的的公司行动信息。\n\n"
            "例如:**@CA问答助手 AVGO**(或 `查 AVGO`)\n\n"
            "弹出内容:\n"
            "• 分红/拆股关键日:宣告 · 登记 · 除息 · 派发(各带距今天数)\n"
            "• 并购/退市/分拆等重大事件 + SEC 原文链接\n"
            "• 现货/合约风控动作 + 运营公告处理提醒\n\n"
            "可查的代码 = 覆盖范围内的标的(发『覆盖』看全部)。"
        )
        return _card("🔎 查代码 用法", "blue",
                     [{"tag": "div", "text": {"tag": "lark_md", "content": content}}],
                     site_url, "打开网页面板")
    cov = next((c for c in data.get("coverage", []) if c["ticker"] == ticker), None)
    cal = [e for e in data.get("calendar", []) if e["ticker"] == ticker]
    name = (cov or {}).get("name", "")
    tags = []
    if cov and cov.get("spot"):
        tags.append("现货")
    if cov and cov.get("contract"):
        tags.append("合约")
    prod = ("[" + "+".join(tags) + "]") if tags else "[未在覆盖范围]"
    if cov and not cov.get("monitored"):
        head_extra = f"\n类型:{cov.get('type_cn','')} —— 商品/海外,无公司行动,仅列入覆盖。"
    elif cov:
        head_extra = f"\n类型:{cov.get('type_cn','')} · 监控中"
    else:
        head_extra = ""
    elems = [{"tag": "div", "text": {"tag": "lark_md",
              "content": f"**{ticker}** {name} {prod}{head_extra}"}}]

    divsplit = [e for e in cal if e["etype"] in ("dividend", "split")]
    filings = [e for e in cal if e["etype"] == "filing"]
    divsplit.sort(key=lambda e: e.get("date") or "")
    filings.sort(key=lambda e: e.get("date") or "", reverse=True)

    def ev_block(e):
        kind = ETYPE_CN.get(e["etype"], e["etype"])
        icon = "💰" if e["etype"] == "dividend" else "✂️"
        lines = [f"**{icon} {kind}{_val(e)}** {('[' + '+'.join(e['products']) + ']') if e.get('products') else ''}"]
        chain = []
        if e.get("decl"):
            chain.append(f"宣告 {_days_str(e['decl'])}")
        if e.get("record"):
            chain.append(f"登记 {_days_str(e['record'])}")
        if e.get("date"):
            chain.append(f"{'除息' if e['etype'] == 'dividend' else '生效'} {_days_str(e['date'])}")
        if e.get("pay"):
            chain.append(f"派发 {_days_str(e['pay'])}")
        lines.append("　" + " · ".join(chain))
        for r in e.get("risk", []):
            lines.append(f"　⚠️ {r}")
        try:
            days = (dt.date.fromisoformat(e["date"]) - dt.date.today()).days if e.get("date") else None
        except Exception:
            days = None
        hint = _ops_hint(days)
        if hint:
            lines.append(f"　📌 {hint}")
        if e.get("etype") == "dividend":
            if e.get("decl_url"):
                lines.append(f"　📄 [宣告 8-K(本次分红)]({e['decl_url']})")
            elif e.get("ir_url"):
                lines.append(f"　🏛 [公司IR 分红页]({e['ir_url']})")
            else:
                lines.append(f"　🔗 [Nasdaq 分红记录](https://www.nasdaq.com/market-activity/stocks/{e['ticker'].lower()}/dividend-history)")
        return "\n".join(lines)

    if divsplit:
        elems.append({"tag": "div", "text": {"tag": "lark_md", "content": "**—— 分红 / 拆股(关键日)——**"}})
        elems.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n\n".join(ev_block(e) for e in divsplit)}})
    if filings:
        fl = []
        for e in filings:
            s = f"**🏛 {e.get('note') or '重大事件'}** {('[' + '+'.join(e['products']) + ']') if e.get('products') else ''} · 申报 {_days_str(e.get('date'))}"
            for r in e.get("risk", []):
                s += f"\n　⚠️ {r}"
            if e.get("url"):
                s += f"\n　📄 [SEC原文]({e['url']})"
            fl.append(s)
        elems.append({"tag": "div", "text": {"tag": "lark_md", "content": "**—— 重大事件(并购/退市/分拆/要约)——**"}})
        elems.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n\n".join(fl)}})
    if not divsplit and not filings:
        elems.append({"tag": "div", "text": {"tag": "lark_md", "content": "近窗口内暂无公司行动记录。"}})

    return _card(f"🔎 {ticker} 公司行动", "blue", elems, site_url, "打开网页面板")


# ---------------- 需求提报 ----------------
def request_card(ok, msg, text="", site_url=""):
    if ok:
        content = (f"✅ 需求已收到,谢谢!已汇总给负责人,会排进迭代评估。\n\n你的需求:{text}"
                   if text else "✅ 需求已收到,谢谢!")
        tpl = "green"
    elif text == "":
        content = "用法:**需求 + 你的想法**,例如「需求 希望增加财报日提醒」。"
        tpl = "blue"
    else:
        content = f"⚠️ 提交未成功:{msg}"
        tpl = "red"
    return _card("📝 需求提报", tpl,
                 [{"tag": "div", "text": {"tag": "lark_md", "content": content}}],
                 site_url, "打开网页面板")


# ---------------- 确认(人工 finalize)----------------
def confirm_card(ok, msg, ticker=None, value=None, site_url="", date=None, etype=None, warn=""):
    """用法:确认 代码 [正确值] [日期] [备注]。日期=该事件的除息日(分红)/生效日(拆股)。
    warn:ADR 防呆提示(确认的值像净额时),非空则红字置顶。"""
    if ok:
        v = f",以 **{value}** 为准" if value is not None else ""
        dd = f"({date_label(etype)} {date})" if date else ""
        head = f"<font color='red'>{warn}</font>\n\n" if warn else ""
        content = (head + f"✅ 已记录确认:**{ticker}**{dd}{v}。\n"
                   "金额门禁解除、停止报警;已写入留痕库(谁/何时/核对来源,发『留痕』可调取)。\n\n"
                   "> 同一标的有多条**值不同**的异常时,请带上日期指定是哪一条,"
                   "例:`确认 KLAC 2.3 2026-05-18`、`确认 KLAC 1.9 2026-02-17`。\n"
                   "> 可在末尾加备注记录你核对了什么,例:`确认 KLAC 2.3 2026-05-18 已比对公司8-K`。")
        tpl = "green"
    else:
        content = (f"⚠️ 确认未成功:{msg}\n\n"
                   "用法:`确认 代码 [正确值] [日期] [备注]`,例:`确认 KLAC 2.3 2026-05-18 已比对公司8-K`")
        tpl = "red"
    return _card("✅ 人工确认", tpl,
                 [{"tag": "div", "text": {"tag": "lark_md", "content": content}}],
                 site_url, "打开网页面板")


# ---------------- 留痕库(确认审计)----------------
def _ago_bj(iso):
    """把 at_bj(ISO 带 +08:00)显示成『MM-DD HH:MM』。"""
    s = iso or ""
    try:
        return f"{s[5:10]} {s[11:16]}"
    except Exception:
        return s


def audit_card(log, site_url="", ticker=None):
    """确认留痕:谁在何时把哪条改成了什么值 + 核对来源 + 备注。log 已按时间倒序。"""
    title = f"📒 确认留痕 · {ticker}" if ticker else "📒 确认留痕(最近确认)"
    if not log:
        tip = (f"暂无 **{ticker}** 的确认记录。" if ticker else "留痕库还没有记录 —— 尚无人工确认。") + \
              "\n每条『确认』都会自动落库(只追加不删),要离线表用 `tools/export_ack_log.py` 导 Excel。"
        return _card(title, "blue", [{"tag": "div", "text": {"tag": "lark_md", "content": tip}}],
                     site_url, "打开网页面板")
    lines = []
    for e in log:
        who = e.get("by_name") or (("…" + e["by"][-6:]) if e.get("by") else "未知")
        val = e.get("value")
        prev = e.get("prev_value")
        vtxt = (f"**{val}**" if val not in (None, "") else "—")
        if prev not in (None, "", val):
            vtxt += f"(原 {prev})"
        et = ETYPE_CN.get(e.get("etype"), e.get("etype") or "")
        dlab = date_label(e.get("etype"))
        head = (f"• {_ago_bj(e.get('at_bj'))}　**{e.get('ticker','')}** {et} "
                f"{dlab} {e.get('date','') or ''} → {vtxt}　_by {who}_")
        sub = []
        if e.get("source"):
            sub.append(f"[核对来源]({e['source']})")
        if e.get("note"):
            sub.append(f"备注:{e['note']}")
        lines.append(head + ("\n　" + " · ".join(sub) if sub else ""))
    body = "\n".join(lines)
    foot = "\n\n_只追加、永不删;完整表用 `tools/export_ack_log.py` 导 Excel。_"
    return _card(title, "blue",
                 [{"tag": "div", "text": {"tag": "lark_md", "content": body + foot}}],
                 site_url, "打开网页面板")
