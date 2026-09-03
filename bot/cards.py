# -*- coding: utf-8 -*-
"""从 Pages 发布的 data.json 构建 Lark 交互卡片。"""
import datetime as dt
from business_time import today as business_today
# 注意:不要在模块顶层 import ack —— ack 依赖 requests,而 CI 的「指令一致性检查」在装依赖**之前**
# 就 import cards,顶层拉 ack 会 ModuleNotFoundError。ack 只在 _authoritative_link 的兜底分支惰性导入。
ETYPE_CN = {"dividend": "分红", "split": "拆股", "filing": "并购/公告"}
PUBLIC_DATA_SCHEMA_VERSION = 4
_SNAPSHOT_REQUIRED_LISTS = (
    "coverage", "pending", "forecasts", "calendar", "announced",
    "recent_declares", "conflicts", "gaps", "filing_updates", "changelog",
)


def validate_snapshot(data, today=None, now=None):
    """校验 Pages→Bot 数据契约；返回空串表示可安全给出业务结论。"""
    if not isinstance(data, dict):
        return "Pages 返回的不是有效 JSON 对象"
    if data.get("_snapshot_error"):
        return str(data["_snapshot_error"])
    if data.get("schema_version") != PUBLIC_DATA_SCHEMA_VERSION:
        got = data.get("schema_version", "缺失")
        return f"数据契约版本不一致（Bot 需要 v{PUBLIC_DATA_SCHEMA_VERSION}，当前为 {got}）"
    for key in _SNAPSHOT_REQUIRED_LISTS:
        if not isinstance(data.get(key), list):
            return f"数据字段 {key} 缺失或格式错误"
    if not isinstance(data.get("counts"), dict):
        return "数据字段 counts 缺失或格式错误"
    for key in ("generated", "generated_at_utc", "valid_until_utc", "business_date", "source_sha"):
        if not data.get(key):
            return f"数据缺少版本字段 {key}"
    try:
        snapshot_day = dt.date.fromisoformat(str(data["business_date"]))
        current_day = today or business_today()
        if snapshot_day > current_day:
            return "数据业务日位于未来，时间口径异常"
        # 计算快照之后已经跨过多少个美东工作日。周五→周一只算 1，允许；
        # 周四→周一算 2，视为流水线至少漏跑一个工作日。
        workdays = 0
        cursor = snapshot_day
        while cursor < current_day:
            cursor += dt.timedelta(days=1)
            if cursor.weekday() < 5:
                workdays += 1
        if workdays > 1:
            return f"数据快照已过期（业务日仍停留在 {snapshot_day}）"
        generated_at = dt.datetime.fromisoformat(
            str(data["generated_at_utc"]).replace("Z", "+00:00")
        )
        valid_until = dt.datetime.fromisoformat(
            str(data["valid_until_utc"]).replace("Z", "+00:00")
        )
        if generated_at.tzinfo is None or valid_until.tzinfo is None:
            return "数据时间字段缺少时区"
        if valid_until <= generated_at:
            return "数据有效时点不晚于生成时间，快照契约异常"
        current_time = now or dt.datetime.now(dt.timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=dt.timezone.utc)
        if generated_at > current_time + dt.timedelta(minutes=10):
            return "数据生成时间位于未来，服务器时钟异常"
        if current_time > valid_until:
            return f"数据已超过有效时点（{data['valid_until_utc']}）"
    except (TypeError, ValueError):
        return "数据时间字段格式错误"
    return ""


def unavailable_card(reason, site_url=""):
    body = ("<font color='red'>当前无法取得可验证的公司行动快照，已停止输出“无风险 / "
            "无需操作”等业务结论。</font>\n\n"
            f"原因：{reason}\n\n请检查 GitHub Actions / Pages，恢复后再查询。")
    return _card("⛔ 公司行动数据不可用", "red",
                 [{"tag": "div", "text": {"tag": "lark_md", "content": body}}],
                 site_url, "打开网页检查")


def write_permission_card(site_url=""):
    body = ("<font color='red'>当前账号没有公司行动写操作权限，本次未修改任何状态。</font>\n\n"
            "请由管理员在 Railway Secret `LARK_WRITE_ALLOWED_OPEN_IDS` 中维护操作员白名单；"
            "查询、日历、风险和审计指令仍可正常使用。")
    return _card("⛔ 写操作未授权", "red",
                 [{"tag": "div", "text": {"tag": "lark_md", "content": body}}],
                 site_url, "打开监控网站")


def _etype_label(event):
    return event.get("event_label") or ETYPE_CN.get(event.get("etype"), event.get("etype"))
# 异常/确认里带的那个日期,到底是哪个关键日:分红=除息日,拆股=生效日,filing=事件日
DATE_LABEL = {"dividend": "除息日", "split": "生效日", "filing": "事件日"}
DATE_LABEL_SHORT = {"dividend": "除息", "split": "生效", "filing": "事件日"}


def _business_date(data=None):
    """优先沿用 Pages 快照的美东业务日，确保 Bot 的 D-N 与生成时口径一致。"""
    raw = (data or {}).get("business_date")
    try:
        return dt.date.fromisoformat(raw) if raw else business_today()
    except (TypeError, ValueError):
        return business_today()


def date_label(etype):
    return DATE_LABEL.get(etype, "关键日")


def _short_date_label(etype):
    return DATE_LABEL_SHORT.get(etype, "关键日")


def _alert_copy(days, etype=None):
    """催办文案(与 config.alert_copy 同口径,内联在 bot 侧,避免 import config —— bot 跑在 bot/ 目录,
    config.py 在仓库根不在其 import 路径)。改文案时两处保持一致。"""
    if days is None:
        return ""
    anchor = _short_date_label(etype)
    if days <= 0:
        return f"🔴 今日{anchor} —— 立即做最终核对并按既定方案执行。"
    if days == 1:
        return f"⏱ 最后确认:距{anchor}仅剩 1 天 —— 确保文案已就绪、定时发送已备好。"
    if days <= 3:
        return f"⏱ 收尾:距{anchor}剩 {days} 天 —— 确保相关文案全部写完。"
    if days <= 7:
        return f"⏱ 催办:距{anchor}剩 {days} 天 —— 准备文案、明确「具体哪天」执行各项操作、完成排期。"
    if days <= 14:
        return f"进入 14 天窗口:距{anchor}剩 {days} 天 —— 每天跟进,确认本次活动安排。"
    return f"提前知会:距{anchor}约 {days} 天 —— 请留意并排入计划(之后 14 天内会每天催)。"


def _authoritative_link(g, refs=None):
    """冲突核对来源。置信度分级:1 公司IR → 2 具体SEC filing → 3 聚合页。
    **要解决冲突时给两个源**:先最权威(T1/T2),再附聚合页快速核对 ——
    尤其 ADR,权威源是本币公告(NT$/DKK),聚合页补上 USD 数值,两边交叉核对。
    refs 传 data.json 的 refs(IR 映射),避免依赖机器人本地能否读到 refs.json。"""
    links = g.get("references") or []
    if links:
        return "　🔗 核对:" + " · ".join(f"[{x['label']}]({x['url']})" for x in links if x.get("url"))
    try:
        import ack  # Bot 容器把 bot/ 作为工作目录。
    except ModuleNotFoundError:  # 仓库测试以 namespace package 导入 bot.cards。
        from bot import ack
    tk, et = g.get("ticker", ""), g.get("etype")
    url, label, tier = ack.verify_link(tk, et, g.get("src_url") or g.get("sec_url"), refs_ir=refs)
    if tier <= 2:   # 有权威源:权威 + 聚合 两个都给
        return f"　🔗 [{label}]({url}) · [聚合快速核对]({ack.quick_look(tk, et)})"
    return f"　🔗 [{label}]({url})"   # 本就只有 T3,不重复


def _reference_line(e, refs=None, indent="　"):
    """统一渲染 run.py 预先生成的核对引用；兼容尚未刷新到新 schema 的 Pages 数据。"""
    if e.get("etype") == "filing":
        url = e.get("url") or e.get("sec_url") or e.get("src_url")
        return indent + f"📄 [SEC原文(本事件)]({url})" if url else ""
    if e.get("etype") != "dividend":
        return ""
    links = e.get("references") or []
    if links:
        return indent + "🔗 核对:" + " · ".join(
            f"[{x['label']}]({x['url']})" for x in links if x.get("url")
        )
    try:
        import ack
    except ModuleNotFoundError:
        from bot import ack
    primary = (e.get("primary_url") or e.get("decl_url") or e.get("ir_url")
               or ack.authoritative_source(e.get("ticker", ""), e.get("etype"), refs))
    return (indent + f"🔗 核对:[官方·公司公告/IR/SEC]({primary}) · "
            f"[第三方·StockAnalysis（交叉核对，可能滞后）]({ack.quick_look(e.get('ticker', ''), e.get('etype'))})")

# ===== 指令唯一来源(改指令只改这里;HELP_TEXT / 关于卡片 / parse_command 都由它生成)=====
# 顺序即匹配优先级。key 必须在 bot.py 的 on_message 里有对应 dispatch 分支。
COMMANDS = [
    # —— 上手/元信息 ——
    {"key": "about",    "kw": ["关于", "介绍", "about"],                 "name": "关于",   "desc": "这是什么、数据源、规则、更新时点"},
    {"key": "help",     "kw": ["帮助", "help"],                          "name": "帮助",   "desc": "显示指令说明"},
    {"key": "changelog","kw": ["最近更新", "更新日志", "更新", "changelog", "版本"], "name": "最近更新", "desc": "最近 3 次版本更新(更多见网页)"},
    # —— 按紧迫度:高 → 低 ——
    {"key": "risk",     "kw": ["风险", "风控", "risk"],                  "name": "风险",   "desc": "当日风控清单(含现货/合约独立动作结论)"},
    {"key": "today",    "kw": ["今日", "今天", "today"],                 "name": "今日",   "desc": "T0 前后24小时的关键日(除息/生效/登记/派发/宣告)"},
    {"key": "announce", "kw": ["新公告", "公告", "announce"],            "name": "新公告", "desc": "最近 5 个宣告的事件(已派发完标『已结束』)"},
    {"key": "week",     "kw": ["本周", "week"],                          "name": "本周",   "desc": "未来 7 个自然日(含今天)的正式公司行动;按事件去重,预测不计"},
    {"key": "upcoming", "kw": ["临近催办", "催办", "临近", "待执行"],      "name": "临近催办", "desc": "距除息/生效≤14天的执行催办 + 数据核验；合约≤3%不进催办"},
    {"key": "forecast", "kw": ["观察", "预测", "等待宣告", "watch"],      "name": "观察预测", "desc": "观察单源预测:观察 CODE 日期 [备注]；临近会推核验提醒，未证实勿执行"},
    {"key": "calendar", "kw": ["日历", "calendar", "cal"],              "name": "日历",   "desc": "当月公司行动月历(图)"},
    {"key": "coverage", "kw": ["覆盖", "资产", "标的", "coverage"],      "name": "覆盖",   "desc": "各标的在现货/合约的覆盖情况"},
    {"key": "lookup",   "kw": ["查代码", "查询", "代码", "查", "ticker", "lookup"], "name": "查代码", "desc": "@我 + 代码(如 AVGO)弹出该标的公司行动;只发『查代码』看用法"},
    {"key": "filing_resolve", "kw": ["备案结论", "确认备案", "排除备案", "filing resolve"], "name": "备案结论", "desc": "按稳定 event_id 把 SEC 条款核验结案为公司行动或普通备案"},
    {"key": "confirm",  "kw": ["确认", "confirm", "已核对"],              "name": "确认",   "desc": "记录人工确认并留痕；异常列表即时标记，金额/3%产品结论由下一轮流水线按口径重算"},
    {"key": "audit",    "kw": ["留痕", "审计", "audit", "log"], "name": "留痕",   "desc": "调取确认/预测观察/备案结论留痕(可加代码只看某标的);要 Excel 用 tools/export_ack_log.py"},
    {"key": "request",  "kw": ["需求", "提报", "反馈", "建议", "feature"], "name": "需求提报", "desc": "提需求:需求 你的想法 —— 匿名写入公开 GitHub，请勿填写敏感信息"},
]
# 注:顺序即匹配优先级 + 展示顺序。帮助不含 "?"(无匹配时默认即回帮助),避免「…?」误判。

def parse_command(text):
    """识别指令；显式写操作优先于备注里的普通关键词。

    例如 ``确认 ... 已比对公司公告`` 必须进入确认，而不能被备注中的“公告”
    劫持。其余自然语言仍按 COMMANDS 顺序做宽松匹配。
    """
    import re
    t = re.sub(r"@_user_\d+|@_all", "", text or "").strip().lower()
    by_key = {command["key"]: command for command in COMMANDS}
    for key in ("filing_resolve", "confirm", "forecast", "request", "audit", "lookup"):
        for keyword in sorted(by_key[key]["kw"], key=len, reverse=True):
            kw = keyword.lower()
            # 中文没有天然词边界，用户常直接输入「确认AAPL」「观察AAPL」或
            # 「需求提报……」。这些显式写操作只要位于消息开头就应锁定路由；
            # 英文仍要求分隔符，避免把 watchlist / confirmation 当成指令。
            if ((not kw.isascii() and t.startswith(kw)) or
                    re.match(rf"^{re.escape(kw)}(?:\s|[:：,，、]|$)", t)):
                return key
    def contains_keyword(keyword):
        kw = keyword.lower()
        if kw.isascii():
            return bool(re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", t))
        return kw in t

    for c in COMMANDS:
        if any(contains_keyword(k) for k in c["kw"]):
            return c["key"]
    return "help"


def _event_key(event):
    return event.get("event_id") or (
        event.get("ticker"), event.get("etype"), event.get("date")
    )


def _is_routine_filing(event):
    """普通 SEC 备案不是公司行动。

    Pages 生成器会先过滤一次；Bot 再做边界防护，避免历史或
    异常快照把财报/高管变动误写成「并购/退市」或风控动作。
    """
    return (event.get("etype") == "filing" and
            event.get("filing_relevant") is False)


def _non_routine(events):
    return [event for event in (events or []) if not _is_routine_filing(event)]


def _verification_kind(event):
    """消费 producer 下发的核验类型；仅为旧快照保留保守回退。"""
    kind = event.get("verification_kind")
    if kind in {"forecast", "filing_terms", "contract_threshold"}:
        return kind
    if event.get("forecast"):
        return "forecast"
    if event.get("follow_up_mode") != "verification":
        return ""
    if event.get("etype") == "filing" and event.get("filing_relevant") is None:
        return "filing_terms"
    return "contract_threshold"


def _verification_prefix(event):
    return {
        "forecast": "🔎 单源核验 · ",
        "filing_terms": "🔎 公司行动条款核验 · ",
        "contract_threshold": "🔎 合约门槛核验 · ",
    }.get(_verification_kind(event), "")


def _filing_resolution_hint(event):
    """给条款核验显示可复制的稳定 ID 与结案指令。"""
    event_id = event.get("event_id") or ""
    if _verification_kind(event) != "filing_terms" or not event_id:
        return ""
    return (f"\n　ID:`{event_id}`\n"
            "　结案:`确认备案 <ID>` 或 `排除备案 <ID>`（公开仓库只记匿名业务结论）")


def _val(x):
    """金额/比例门禁:有未确认冲突 → 不给确定值,标『待人工确认·勿据此执行』。
    人工发「确认 代码 值」消解冲突后,才恢复显示确定值。"""
    if x.get("forecast"):
        return " <font color='orange'>🔎单源待核实·勿执行</font>"
    if x.get("disputed") and not x.get("acked"):
        vals = x.get("dispute_vals") or {}
        pairs = " / ".join(str(v) for v in dict.fromkeys(vals.values()))
        return f" <font color='red'>⚠️各源不一致({pairs})· 待人工确认,勿据此执行</font>"
    unverified = x.get("value_verified") is False or (
        "value_verified" not in x and not x.get("official") and not x.get("acked")
        and (x.get("amt_srcs") or 0) == 1
    )
    if unverified and (x.get("amount") is not None or x.get("ratio")):
        v = x.get("amount") if x.get("amount") is not None else x.get("ratio")
        return f" <font color='orange'>⚠️单源未交叉验证({v})· 待人工确认,勿据此执行</font>"
    if x.get("value_display"):
        return " " + str(x["value_display"])
    if x.get("amount") is not None:
        return f" ${x['amount']}"
    if x.get("ratio"):
        return f" {x['ratio']}"
    return ""


def _dates(x):
    """关键日链:宣告 · 登记 · 除息/生效 · 派发(有哪个显示哪个,与查代码口径一致)。"""
    lab = _short_date_label(x.get("etype"))
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


def _risk_lines(x):
    return "".join(f"\n　⚠️ {risk}" for risk in x.get("risk", []))


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
    pending = _non_routine(data.get("pending", []))
    forecasts = [e for e in _non_routine(data.get("calendar", [])) if e.get("forecast")]
    gen = data.get("generated", "")
    if not pending and not forecasts:
        elems = [{"tag": "div", "text": {"tag": "lark_md", "content": "近期暂无未来公司行动或待核实事项。"}}]
        return _card(f"📅 公司行动日历 · {gen}", "blue", elems, site_url, "打开网页日历")
    lines = []
    for x in pending[:30]:
        prod = ("[" + "+".join(x["products"]) + "] ") if x.get("products") else ""
        color = "green" if x.get("follow_up_mode") == "none" else (
            "orange" if x.get("follow_up_mode") == "verification" else "red"
        )
        line = (f"• {_verification_prefix(x)}{prod}**{x['ticker']}** {_etype_label(x)}{_val(x)} — "
                f"<font color='{color}'>还剩 {x['days']} 天</font>\n　{_dates(x)}")
        line += _risk_lines(x)
        ref = _reference_line(x, data.get("refs", {}))
        lines.append(line + ("\n" + ref if ref else ""))
    if len(pending) > 30:
        lines.append(f"…… 已展示前 30 条未来事项（含条款核验），共 {len(pending)} 条；完整清单见网页日历。")
    if forecasts:
        for x in forecasts:
            line = (f"• **{x['ticker']}** {_etype_label(x)} "
                    f"<font color='orange'>🔎预测观察·不执行</font>\n　预计 {_dates(x)}")
            ref = _reference_line(x, data.get("refs", {}))
            lines.append(line + ("\n" + ref if ref else ""))
    elems = [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}]
    return _card(f"📅 公司行动日历 · {gen}", "blue", elems, site_url, "打开网页日历")


def alert_card(data, site_url):
    c = dict(data.get("counts", {}))
    # 不盲信上游计数：即使历史 Pages 快照还含例行备案，
    # Bot 也不能把它们计入「新发现/冲突/待执行」。
    for key in ("pending", "forecasts", "new", "conflicts", "gaps", "announced"):
        if isinstance(data.get(key), list):
            c[key] = len(_non_routine(data[key]))
    filing_updates = data.get("filing_updates", [])
    c["filing_updates"] = len(filing_updates) if isinstance(filing_updates, list) else 0
    gen = data.get("generated", "")
    template = "red" if (c.get("conflicts") or c.get("gaps")) else "blue"
    elems = [{"tag": "div", "text": {"tag": "lark_md",
              "content": f"📣 新公告 **{c.get('announced',0)}**　📋 未来跟踪 **{c.get('pending',0)}**　🆕 新发现 **{c.get('new',0)}**"
                         f"　🔄 条款状态 **{c.get('filing_updates',0)}**　❗冲突 **{c.get('conflicts',0)}**　🕳 空缺 **{c.get('gaps',0)}**　🔎 预测 **{c.get('forecasts',0)}**"}},
             {"tag": "hr"}]

    def sec(title, lines):
        if lines:
            more = f"\n…… 已展示前 20 条，共 {len(lines)} 条；完整清单见网页。" if len(lines) > 20 else ""
            elems.append({"tag": "div", "text": {"tag": "lark_md",
                         "content": f"**{title}**\n" + "\n".join(lines[:20]) + more}})

    update_lines = []
    for item in filing_updates if isinstance(filing_updates, list) else []:
        kind = item.get("kind")
        if kind == "confirmed":
            conclusion = "✅ 条款核验已确认，已转正式公司行动"
            if item.get("follow_up_mode") == "execution":
                conclusion += "；请及时处理"
        elif kind == "routine":
            conclusion = "✅ 普通备案，不属于公司行动；本次无需操作"
        elif kind == "linked":
            conclusion = "🔗 已关联至同日已确认分红，仅作为 SEC 证据；不单独操作"
        elif kind == "expired":
            conclusion = ("⌛ 元数据提示超过核验期仍无证据，已停止每日提醒；"
                          "事项仍未核实，不得据此判断无需操作或执行")
        else:
            conclusion = f"🔎 条款仍待核实；事件日已过 {abs(item.get('days') or 0)} 天，勿执行"
        line = (f"• **{item.get('ticker')}** {_etype_label(item)} — {conclusion}\n"
                f"　{_short_date_label(item.get('etype'))} {item.get('date')}")
        if item.get("note"):
            line += f"\n　📝 {item['note']}"
        line += _risk_lines(item)
        ref = _reference_line(item, data.get("refs", {}))
        update_lines.append(line + ("\n" + ref if ref else ""))
    sec("🔄 公司行动条款状态更新", update_lines)

    # 精简为「当日总览」:只给数据质量(冲突/空缺),明细交给专项指令
    conf = [f"• **{g['ticker']}** {_etype_label(g)} {g['date']}: "
            + "; ".join(g.get("conflicts", [])) + _risk_lines(g)
            for g in _non_routine(data.get("conflicts", []))]
    sec("❗ 字段冲突(零容忍)", conf)
    gap = [f"• **{g['ticker']}** {_etype_label(g)} {g['date']}: " + "; ".join(g.get("gaps", []))
           for g in _non_routine(data.get("gaps", []))]
    sec("🕳 数据空缺", gap)

    elems.append({"tag": "div", "text": {"tag": "lark_md",
                  "content": "明细查询:**风险** / **今日** / **本周** / **新公告** / **观察预测** / **覆盖**"}})
    return _card(f"🔔 当日总览 · {gen}", template, elems, site_url, "打开预警面板")


# 由 COMMANDS 自动生成(勿手改)
HELP_TEXT = "可用指令(@我 + 关键词):\n" + "\n".join(
    f"• **{c['name']}** —— {c['desc']}" for c in COMMANDS)

# 关于卡片里的指令名清单(由 COMMANDS 生成)
COMMAND_NAMES = " · ".join(c["name"] for c in COMMANDS)


# ---------------- 关于 / 介绍 ----------------
def about_card(data, site_url):
    gen = data.get("generated", "")
    cov = data.get("coverage", [])
    n_spot = sum(1 for x in cov if x.get("spot"))
    n_contract = sum(1 for x in cov if x.get("contract"))
    n_monitored = sum(1 for x in cov if x.get("monitored"))
    data_sha = str(data.get("source_sha") or "unknown")[:12]
    bot_sha = str(data.get("_bot_build_sha") or "unknown")[:12]
    content = (
        "**CA问答助手** —— 公司行动(Corporate Actions)监控\n"
        f"盯 **现货({n_spot} 支)+ 合约范围({n_contract} 个)**标的中的 **{n_monitored} 个可监控证券**:分红 / 拆股·合股 / 并购 / 分拆 / 退市·代码变更。"
        "合约里的个股与 ETF(QQQ/EWY/DRAM/TQQQ/MVLL)均纳入监控；商品(XAU/WTI/XAG/BRENTOIL/NATGAS/XCU)无公司行动，仅列入覆盖。\n\n"
        "**数据源(8 源,多源交叉核对·零容忍)**\n"
        "yfinance · FMP · Alpha Vantage · Nasdaq · Tiingo · Alpaca · SEC EDGAR · FINX(TRKD-HS)\n\n"
        "**SEC filing 分流**:8-K / 8-K/A 中破产/接管、完成收购或资产处置、退市、证券权利或控制权变更等明确结构性事项进入公司行动流；"
        "财报、融资协议、高管变动等普通备案只留在网站 SEC 原文表。6-K / 6-K/A 仅在文件名或描述命中强提示时进入『公司行动条款核验』，"
        "核实前不计为正式公司行动，也不会触发执行或正式 @。\n\n"
        "**关键日**:每条事件展示 **宣告 · 登记 · 除息/生效 · 派发**(缺哪个不显示哪个)。\n\n"
        "**取值规则**:金额/比例取 **多数票 + 源优先级**(要的是公司宣告的原值)。"
        "注意各源口径不同 —— yfinance 会按拆股回溯调整历史分红、还四舍五入;Alpaca 对 ADR 报的是扣预扣税后的净额。\n\n"
        "**🚦 金额门禁(重要)**:只有**多源交叉验证过且无冲突**的金额才显示确定值。否则一律封锁:\n"
        "• 各源不一致 → `⚠️各源不一致(a / b)· 待人工确认,勿据此执行`\n"
        "• 只有 1 个源报 → `⚠️单源未交叉验证(x)· 待人工确认,勿据此执行`\n"
        "**没确认过的数字,不要拿去执行。**\n\n"
        "**🔎 预测观察**:单源且未见宣告日的预估不会进入正式执行催办;可发 `观察 CODE 日期 [备注]` 重点跟踪。"
        "进入 30 天窗口知会一次、14 天内每日推数据核验提醒；公司宣告/第二个独立源、改期或失效也会主动推送。**预测不得执行。**\n\n"
        "**📐 合约操作门槛**:公司行动仍正常报告；现金分红按每股毛额÷前一完整交易日未调整收盘价估算，"
        "送股、拆股、合股按条款估算理论除权价影响。只有严格 **>3%** 才标记合约需操作；"
        "3% 或以下明确显示『合约：本次无需操作』。缺金额、比例、币种/单位或参考价时只做门槛核验。现货流程独立。\n\n"
        "**🙋 人工介入闭环(零容忍·不豁免)**:字段冲突 / 数据空缺,"
        "**每次扫描都重报、一直挂着**,并显示「已挂 N 天」;超 3 天没人确认会在推送里 **@ 负责人**。"
        "消解方式:群里发 **确认 代码 [正确值] [日期] [备注]**(如 `确认 AAPL 0.26 2026-08-11`;"
        "拆/合股用完整比例，如 `确认 XYZ 1:10 2026-09-10`;同一标的多条不同值时必须带日期)"
        "—— 确认会即时写入生效库并标记异常；金额与 3% 产品结论由下一轮流水线结合币种、单位和参考价重算，资料不足时仍保留核验提醒。"
        "每次确认**只追加、不删**地写入留痕库(谁/何时/改值前后/核对来源/备注),"
        "群里发 **留痕** 可随时调取,离线表用 `tools/export_ack_log.py` 导 Excel。\n\n"
        "**核对链接**:并购/退市直达 SEC 原文；分红统一给 **官方本次公告/IR/SEC** + **StockAnalysis 交叉核对**。第三方可能滞后，不能作为正式化依据。\n\n"
        "**更新**:每交易日 3 次 —— 开盘后 9:35 / 盘中 12:45 / 收盘后 16:05(美东)。\n\n"
        "**提前预警**:已宣告或双源确认后先成为正式事项，再按现货流程/合约 3% 结论决定是否进入 **30/14** 催办；"
        "单源预测只按相同节奏推核验并明确『勿执行』。\n\n"
        f"**指令**(@我 + 关键词):{COMMAND_NAMES}\n\n"
        f"_数据更新于 {gen} · Pages {data_sha} · Bot {bot_sha}_"
    )
    return _card("ℹ️ 关于 CA问答助手", "blue",
                 [{"tag": "div", "text": {"tag": "lark_md", "content": content}}],
                 site_url, "打开网页面板")


def _line(e, with_days=True, with_risk=False):
    prod = ("[" + "+".join(e["products"]) + "] ") if e.get("products") else ""
    prefix = _verification_prefix(e)
    d = e.get("date") or ""
    if e.get("etype") == "filing" and e.get("note"):
        # filing 的 date = SEC 申报日(非执行日),显式标出以免「无日期」误以为紧急
        datestr = f" · 申报 {d}" if d else ""
        s = f"• {prefix}{prod}**{e['ticker']}** {e['note']}{datestr}"
        if e.get("url"):
            s += f" [SEC原文]({e['url']})"
    else:
        label = _short_date_label(e.get("etype"))
        datestr = f" · {label} {d}" if d else ""
        s = f"• {prefix}{prod}**{e['ticker']}** {_etype_label(e)}{_val(e)}{datestr}"
    if with_risk:
        for r in e.get("risk", []):
            s += f"\n　⚠️ {r}"
    s += _filing_resolution_hint(e)
    return s


# ---------------- 风险(风控清单)----------------
def risk_card(data, site_url):
    business_date = _business_date(data)
    today = business_date.isoformat()
    lo30 = (business_date - dt.timedelta(days=30)).isoformat()
    cal = _non_routine(data.get("calendar", []))
    splits = [
        e for e in cal
        if e["etype"] == "split" and (e.get("date") or "") >= today
        and ("现货" in (e.get("products") or []) or
             (e.get("contract_action") or {}).get("status") in ("required", "review"))
    ]
    dividends = [
        e for e in cal
        if e.get("etype") == "dividend" and (e.get("date") or "") >= today
        and ("现货" in (e.get("products") or []) or
             (e.get("contract_action") or {}).get("status") in ("required", "review"))
    ]
    filing_reviews = [
        e for e in cal
        if e["etype"] == "filing" and (e.get("date") or "") >= lo30
        and _verification_kind(e) == "filing_terms"
    ]
    structurals = [
        e for e in cal
        if e["etype"] == "filing" and (e.get("date") or "") >= lo30
        and _verification_kind(e) != "filing_terms"
    ]
    conflicts = _non_routine(data.get("conflicts", []))
    n = len(dividends) + len(splits) + len(structurals) + len(filing_reviews) + len(conflicts)
    template = "red" if n else "green"
    elems = [{"tag": "div", "text": {"tag": "lark_md",
              "content": f"当日风控总览 · 分红处理/核验 **{len(dividends)}** · 拆股 **{len(splits)}** · 已确认结构事项 **{len(structurals)}** · 条款核验 **{len(filing_reviews)}** · 数据冲突 **{len(conflicts)}**"}},
             {"tag": "hr"}]

    def sec(title, lines):
        if lines:
            more = f"\n…… 已展示前 20 条，共 {len(lines)} 条；完整清单见网页。" if len(lines) > 20 else ""
            elems.append({"tag": "div", "text": {"tag": "lark_md",
                         "content": f"**{title}**\n" + "\n".join(lines[:20]) + more}})

    sec("💸 分红(现货照常处理；合约>3%需操作，缺价/缺值待核实)",
        [_line(e, with_risk=True) for e in dividends])
    sec("✂️ 拆股/合股(>3%需操作；比例不足则待核实)",
        [_line(e, with_risk=True) for e in splits])
    sec("🤝 已确认结构性行动(并购 / 退市 / 分拆等)",
        [_line(e, with_risk=True) for e in structurals])
    sec("🔎 疑似公司行动(条款核验·勿执行)",
        [_line(e, with_risk=True) for e in filing_reviews])
    _refs = data.get("refs", {})

    def _conf_line(g):
        s = (f"• **{g['ticker']}** {_etype_label(g)} {date_label(g['etype'])} {g['date']}: "
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
def _window_card(data, site_url, lo_days, hi_days, title, *, anchor_only=False):
    today = _business_date(data)
    lo = (today + dt.timedelta(days=lo_days)).isoformat()
    hi = (today + dt.timedelta(days=hi_days)).isoformat()
    cal = _non_routine(data.get("calendar", []))
    events = {}
    filing_reviews = {}
    excluded_forecasts = set()
    for e in cal:
        event_key = _event_key(e)
        if e.get("forecast"):
            # 「本周/今日」只统计正式事项；预测有独立的「观察预测」入口。
            keys = ((e.get("date"),) if anchor_only else
                    (e.get("date"), e.get("record"), e.get("pay"), e.get("decl")))
            if any(d and lo <= d <= hi for d in keys):
                excluded_forecasts.add(event_key)
            continue
        is_filing_review = _verification_kind(e) == "filing_terms"
        # 命中:除息/生效/公告(date)、登记、派发、宣告 任一落在 [lo, hi] 窗口内
        anchor_label = DATE_LABEL.get(e.get("etype"), "关键日")
        keys = {anchor_label: e.get("date")}
        if not anchor_only:
            keys.update({"登记日": e.get("record"), "派发日": e.get("pay"),
                         "宣告日": e.get("decl")})
        for label, d in keys.items():
            if d and lo <= d <= hi:
                target = filing_reviews if is_filing_review else events
                item = target.setdefault(event_key, {"event": e, "milestones": {}})
                item["milestones"].setdefault(d, []).append(label)
    ordered = sorted(events.values(), key=lambda x: min(x["milestones"]))
    review_ordered = sorted(filing_reviews.values(), key=lambda x: min(x["milestones"]))
    date_basis = "按除息/生效日、" if anchor_only else "按关键日、"
    scope = (f"口径：美东业务日 **{lo} 至 {hi}**，{date_basis}按公司行动事件去重；"
             f"正式事项 **{len(ordered)}** 个，预测不计。")
    if review_ordered:
        scope += (f"另有 **{len(review_ordered)}** 个疑似事项进入公司行动条款核验，"
                  "不计入正式事项、核实前勿执行。")
    if excluded_forecasts:
        scope += f"窗口内另有 **{len(excluded_forecasts)}** 个预测，请看「观察预测」。"
    if not ordered and not review_ordered:
        body = scope + f"\n\n{title}暂无正式公司行动关键日。"
        return _card(f"🗓 {title}", "green",
                     [{"tag": "div", "text": {"tag": "lark_md", "content": body}}], site_url, "打开网页日历")
    lines = []
    display_items = [(False, item) for item in ordered] + [
        (True, item) for item in review_ordered
    ]
    display_items.sort(key=lambda row: min(row[1]["milestones"]))
    for is_review, item in display_items[:40]:
        e = item["event"]
        milestones = item["milestones"]
        prod = ("[" + "+".join(e["products"]) + "] ") if e.get("products") else ""
        flag = "🔴 今天 " if today.isoformat() in milestones else ""
        dates = " · ".join(
            f"{d} **{'、'.join(dict.fromkeys(labels))}**"
            for d, labels in sorted(milestones.items())
        )
        prefix = _verification_prefix(e)
        line = f"• {flag}{prefix}{prod}**{e['ticker']}** {_etype_label(e)}{_val(e)} —— {dates}"
        if is_review:
            line += "\n　👉 请打开 SEC 原文确认事件类型、生效日与处理条款；核实前勿执行。"
            line += _filing_resolution_hint(e)
        line += _risk_lines(e)
        ref = _reference_line(e, data.get("refs", {}))
        lines.append(line + ("\n" + ref if ref else ""))
    if len(display_items) > 40:
        lines.append(f"…… 已展示前 40 个事项，共 {len(display_items)} 个；完整清单见网页日历。")
    elems = [{"tag": "div", "text": {"tag": "lark_md", "content": scope + "\n\n" + "\n".join(lines)}}]
    return _card(f"🗓 {title}", "blue", elems, site_url, "打开网页日历")


def today_card(data, site_url):
    # T0 ±24 小时:昨天/今天/明天 的关键日
    return _window_card(data, site_url, -1, 1, "今日(前后24小时)")


def week_card(data, site_url):
    # 含今天共 7 个自然日，因此闭区间是 D+0 ... D+6。
    return _window_card(
        data, site_url, 0, 6, "本周(未来7个自然日)", anchor_only=True
    )


def upcoming_card(data, site_url):
    """临近提醒:0–14 天执行催办 + 数据核验；不等同于本周 7 天窗口。"""
    gen = data.get("generated", "")
    pend = sorted(
        (x for x in _non_routine(data.get("pending", []))
         if isinstance(x.get("days"), int) and 0 <= x["days"] <= 14
         and x.get("follow_up_mode", "execution") != "none"),
        key=lambda x: x["days"],
    )
    formal_sigs = {_event_key(x) for x in pend}
    forecasts = sorted(
        (x for x in _non_routine(data.get("forecasts", []))
         if isinstance(x.get("days"), int) and 0 <= x["days"] <= 14
         and _event_key(x) not in formal_sigs),
        key=lambda x: x["days"],
    )
    items = sorted(
        [(x["days"], False, x) for x in pend] + [(x["days"], True, x) for x in forecasts],
        key=lambda row: (row[0], row[1], row[2].get("ticker", "")),
    )
    if not items:
        content = ("未来 14 天暂无执行催办或数据核验提醒。合约无需操作的事项仍可在『日历/查代码』查看。\n\n"
                   "口径：按除息/生效日计算；与「本周（未来7天）」分开。30 天首次知会由定时推送触发。")
        elems = [{"tag": "div", "text": {"tag": "lark_md", "content": content}}]
        return _card(f"🔔 临近提醒(≤14天) · {gen}", "blue", elems, site_url, "打开网页面板")
    lines = []
    for _, is_forecast, x in items[:30]:
        prod = ("[" + "+".join(x["products"]) + "] ") if x.get("products") else ""
        verification_kind = "forecast" if is_forecast else _verification_kind(x)
        prefix = {
            "forecast": "🔎 单源核验 · ",
            "filing_terms": "🔎 公司行动条款核验 · ",
            "contract_threshold": "🔎 合约门槛核验 · ",
        }.get(verification_kind, "")
        color = "orange" if verification_kind else "red"
        display = {**x, "forecast": True} if is_forecast else x
        line = (f"• {prefix}{prod}**{x['ticker']}** {_etype_label(x)}{_val(display)} — "
                f"<font color='{color}'>还剩 {x['days']} 天</font>\n　{_dates(x)}")
        srcs = x.get("srcs") or []
        if srcs:
            n = len(srcs)
            tag = "单源" if n == 1 else f"{n}源"
            line += f"\n　📡 数据源({tag}):{', '.join(srcs)}"
        if is_forecast:
            line += "\n　👉 请核对公司官方公告或第二个独立源；未确认前勿执行。"
        elif verification_kind == "filing_terms":
            line += "\n　👉 请打开 SEC 原文确认事件类型、生效日与处理条款；核实前勿执行。"
            line += _filing_resolution_hint(x)
        elif verification_kind == "contract_threshold":
            line += "\n　👉 请补齐可靠金额/比例或参考价；确认影响严格超过 3% 前不执行合约调整。"
        elif x.get("days") is not None:
            line += f"\n　👉 {_alert_copy(x['days'], x.get('etype'))}"
        line += _risk_lines(x)
        ref = _reference_line(x, data.get("refs", {}))
        if ref:
            line += "\n" + ref
        lines.append(line)
    if len(items) > 30:
        lines.append(f"…… 已展示前 30 条，共 {len(items)} 条；完整提醒清单见网页面板。")
    execution_count = sum(1 for x in pend if x.get("follow_up_mode", "execution") == "execution")
    filing_review_count = sum(1 for x in pend if _verification_kind(x) == "filing_terms")
    contract_review_count = sum(1 for x in pend if _verification_kind(x) == "contract_threshold")
    scope = (f"口径：距除息/生效 **0–14 天**，执行催办 **{execution_count}** 个、"
             f"公司行动条款核验 **{filing_review_count}** 个、"
             f"合约门槛核验 **{contract_review_count}** 个、单源核验 **{len(forecasts)}** 个；核验事项勿执行。"
             "与「本周（未来7天）」分开。30 天首次知会由定时推送触发。")
    elems = [{"tag": "div", "text": {"tag": "lark_md", "content": scope + "\n\n" + "\n".join(lines)}}]
    return _card(f"🔔 临近提醒(≤14天)· {gen}", "blue", elems, site_url, "打开网页面板")


def forecast_card(data, site_url):
    """待核实预测：会推临近核验提醒，但不得据此执行。"""
    gen = data.get("generated", "")
    forecasts = sorted(_non_routine(data.get("forecasts", [])),
                       key=lambda x: x.get("days", 9999))
    if not forecasts:
        body = ("当前没有待核实预测。\n"
                "标记用法:`观察 CODE YYYY-MM-DD [备注]`，例:`观察 AAPL 2026-08-11 等待公司宣告`")
        return _card(f"🔎 预测观察 · {gen}", "green",
                     [{"tag": "div", "text": {"tag": "lark_md", "content": body}}],
                     site_url, "打开网页面板")
    lines = []
    for x in forecasts[:30]:
        prod = ("[" + "+".join(x["products"]) + "] ") if x.get("products") else ""
        srcs = ", ".join(x.get("srcs") or []) or "未知"
        watch = "👁 已人工标记观察" if x.get("watching") else "🔎 自动识别预测"
        line = (f"• {prod}**{x['ticker']}** {_etype_label(x)} — "
                f"<font color='orange'>{watch} · 数值待核实 · 不执行</font>\n"
                f"　预计 {_dates(x)} · 数据源:{srcs}\n"
                "　👉 临近会按 30/14 天节奏推核验提醒；确认后转正式事项，再按产品动作结论决定是否催办")
        if x.get("watch_note"):
            line += f"\n　📝 {x['watch_note']}"
        ref = _reference_line(x, data.get("refs", {}))
        if ref:
            line += "\n" + ref
        lines.append(line)
    if len(forecasts) > 30:
        lines.append(f"…… 已展示前 30 条，共 {len(forecasts)} 条；完整清单见网页面板。")
    return _card(f"🔎 预测观察 · {gen}", "orange",
                 [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}],
                 site_url, "打开网页面板")


def forecast_mark_card(ok, message, ticker="", date="", site_url=""):
    template = "green" if ok else "red"
    title = "👁 已进入预测观察" if ok else "⚠️ 无法标记预测观察"
    body = message
    if ok and ticker:
        body += (f"\n\n**{ticker}** · 预计 {date}\n会按临近节奏推数据核验提醒；"
                 "未证实前不进入正式执行催办，升级、改期或失效也会主动通知。")
    return _card(title, template,
                 [{"tag": "div", "text": {"tag": "lark_md", "content": body}}],
                 site_url, "打开网页面板")


def announce_card(data, site_url):
    # 最近 5 个被宣告(declaration date)的事件;已派发完的标「已结束」
    ann = _non_routine(data.get("recent_declares") or data.get("announced", []))
    if not ann:
        return _card("📣 新公告", "green",
                     [{"tag": "div", "text": {"tag": "lark_md", "content": "近期暂无宣告事件。"}}],
                     site_url, "打开网页面板")
    lines = []
    for x in ann[:5]:
        prod = ("[" + "+".join(x["products"]) + "] ") if x.get("products") else ""
        prefix = _verification_prefix(x)
        if x.get("ended"):
            status = " · ✅ 已结束"
        elif x.get("days") is not None and x["days"] >= 0:
            status = f" · 还剩 {x['days']} 天"
        else:
            status = ""
        line = (f"• {prefix}{prod}**{x['ticker']}** {_etype_label(x)}{_val(x)} —— "
                f"宣告 {x.get('decl')} · {_short_date_label(x.get('etype'))} {x['date']}{status}")
        line += _risk_lines(x)
        ref = _reference_line(x, data.get("refs", {}))
        lines.append(line + ("\n" + ref if ref else ""))
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
        # 同一版本的修复项必须完整展示；此前固定 [:6] 会把第 7 条（恰好是
        # 去重缓存修复）静默藏掉，造成 Pages 有记录而 Bot 看不到。
        items = "\n".join(f"　• {i}" for i in e["items"])
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
def is_monitored_ticker(data, ticker):
    """ticker 是否是当前 Pages 覆盖内、可生成公司行动的证券。"""
    return any(c.get("ticker") == ticker and c.get("monitored")
               for c in data.get("coverage", []) or [])


def find_ticker(text, data):
    """从消息里抽出一个『已覆盖』的标的代码(忽略 @、指令词)。无则 None。"""
    import re
    known = {c["ticker"] for c in data.get("coverage", [])}
    aliases = {str(k).upper(): str(v).upper()
               for k, v in (data.get("ticker_aliases") or {}).items()}
    # 支持 BRK-B / BRK.B 等带分隔符代码及 BRENTOIL 等长代码；最后仍必须命中 Pages 覆盖。
    toks = re.findall(r"[A-Za-z][A-Za-z0-9.-]*", (text or "").upper())
    for t in toks:
        canonical = aliases.get(t, t)
        if canonical in known:
            return canonical
    return None


def _sec_company_url(ticker):
    # EDGAR 的 CIK 参数可直接用代码解析到公司,列出该标的全部备案
    return ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
            f"&CIK={ticker}&type=&dateb=&owner=include&count=40")


def _days_str(d, today=None):
    if not d:
        return ""
    try:
        n = (dt.date.fromisoformat(d) - (today or business_today())).days
    except Exception:
        return d
    rel = "今天" if n == 0 else (f"{n}天后" if n > 0 else f"{-n}天前")
    return f"{d}({rel})"


def _ops_hint(days, etype=None):
    """按距除息/生效天数给运营公告处理提醒(与预警节奏 30/14/7/3/1 一致)。"""
    if days is None or days < 0:
        return ""
    anchor = _short_date_label(etype)
    if days == 0:
        return f"运营:今日{anchor} —— 立即做最终核对并按既定方案执行"
    if days == 1:
        return f"运营:最后确认 —— 距{anchor}仅剩 1 天，公告文案就绪、定时发送已设置"
    if days <= 3:
        return f"运营·催办:距{anchor}剩 {days} 天，确保公告文案全部写完"
    if days <= 7:
        return f"运营·催办:距{anchor}剩 {days} 天，开始准备公告文案,明确各项执行的具体日期/排期"
    if days <= 14:
        return f"运营:距{anchor}剩 {days} 天，提前知会,确认本次活动安排"
    if days <= 30:
        return f"运营:距{anchor}约 {days} 天，提前知会,留意并排入计划"
    return ""


def lookup_card(data, ticker, site_url):
    if not ticker:
        content = (
            "**用法:@CA问答助手 + 空格 + 标的代码**,即可弹出该标的的公司行动信息。\n\n"
            "例如:**@CA问答助手 AVGO**(或 `查 AVGO`)\n\n"
            "弹出内容:\n"
            "• 分红/拆股关键日:宣告 · 登记 · 除息/生效 · 派发(各带距今天数)\n"
            "• 已确认结构性行动，以及疑似事项的条款核验 + SEC 原文链接\n"
            "• 现货/合约风控动作 + 运营公告处理提醒\n\n"
            "可查的代码 = 覆盖范围内的标的(发『覆盖』看全部)。"
        )
        return _card("🔎 查代码 用法", "blue",
                     [{"tag": "div", "text": {"tag": "lark_md", "content": content}}],
                     site_url, "打开网页面板")
    today = _business_date(data)
    cov = next((c for c in data.get("coverage", []) if c["ticker"] == ticker), None)
    cal = [e for e in _non_routine(data.get("calendar", [])) if e["ticker"] == ticker]
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
    filing_reviews = [
        e for e in cal
        if e["etype"] == "filing" and _verification_kind(e) == "filing_terms"
    ]
    filings = [
        e for e in cal
        if e["etype"] == "filing" and _verification_kind(e) != "filing_terms"
    ]
    divsplit.sort(key=lambda e: e.get("date") or "")
    filings.sort(key=lambda e: e.get("date") or "", reverse=True)

    def ev_block(e):
        kind = _etype_label(e)
        icon = "💰" if e["etype"] == "dividend" else "✂️"
        lines = [f"**{icon} {kind}{_val(e)}** {('[' + '+'.join(e['products']) + ']') if e.get('products') else ''}"]
        chain = []
        if e.get("decl"):
            chain.append(f"宣告 {_days_str(e['decl'], today)}")
        if e.get("record"):
            chain.append(f"登记 {_days_str(e['record'], today)}")
        if e.get("date"):
            chain.append(f"{'除息' if e['etype'] == 'dividend' else '生效'} {_days_str(e['date'], today)}")
        if e.get("pay"):
            chain.append(f"派发 {_days_str(e['pay'], today)}")
        lines.append("　" + " · ".join(chain))
        if e.get("forecast"):
            lines.append("　🔎 单源预测观察，临近会推核验提醒；未证实前不执行。")
        else:
            for r in e.get("risk", []):
                lines.append(f"　⚠️ {r}")
        try:
            days = (dt.date.fromisoformat(e["date"]) - today).days if e.get("date") else None
        except Exception:
            days = None
        mode = e.get("follow_up_mode", "execution")
        if e.get("forecast") or mode == "none":
            hint = ""
        elif _verification_kind(e) == "filing_terms":
            hint = "公司行动条款核验；请核对 SEC 原文中的事件类型、生效日与处理条款，核实前勿执行"
        elif mode == "verification":
            hint = "合约门槛待核实；补齐金额/比例或参考价前不执行调整"
        else:
            hint = _ops_hint(days, e.get("etype"))
        if hint:
            lines.append(f"　📌 {hint}")
        ref = _reference_line(e, data.get("refs", {}))
        if ref:
            lines.append(ref)
        return "\n".join(lines)

    if divsplit:
        elems.append({"tag": "div", "text": {"tag": "lark_md", "content": "**—— 分红 / 拆股(关键日)——**"}})
        elems.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n\n".join(ev_block(e) for e in divsplit)}})
    def filing_lines(items):
        fl = []
        for e in items:
            s = f"**🏛 {e.get('note') or '重大事件'}** {('[' + '+'.join(e['products']) + ']') if e.get('products') else ''} · 申报 {_days_str(e.get('date'), today)}"
            if _verification_kind(e) == "filing_terms":
                s += "\n　📌 公司行动条款核验：请确认事件类型、生效日与处理条款；核实前勿执行。"
                s += _filing_resolution_hint(e)
            for r in e.get("risk", []):
                s += f"\n　⚠️ {r}"
            if e.get("url"):
                s += f"\n　📄 [SEC原文]({e['url']})"
            fl.append(s)
        return fl

    if filings:
        elems.append({"tag": "div", "text": {"tag": "lark_md", "content": "**—— 已确认结构性行动(并购/退市/分拆/要约)——**"}})
        elems.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n\n".join(filing_lines(filings))}})
    if filing_reviews:
        elems.append({"tag": "div", "text": {"tag": "lark_md", "content": "**—— 疑似公司行动(条款核验·勿执行)——**"}})
        elems.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n\n".join(filing_lines(filing_reviews))}})
    if not divsplit and not filings and not filing_reviews:
        elems.append({"tag": "div", "text": {"tag": "lark_md", "content": "近窗口内暂无公司行动记录。"}})

    return _card(f"🔎 {ticker} 公司行动", "blue", elems, site_url, "打开网页面板")


# ---------------- 需求提报 ----------------
def request_card(ok, msg, text="", site_url=""):
    if ok:
        content = (f"✅ 需求已收到,谢谢!已汇总给负责人,会排进迭代评估。\n\n你的需求:{text}"
                   if text else "✅ 需求已收到,谢谢!")
        content += "\n\n> 需求正文会进入公开 GitHub；系统不会保存你的 Lark 身份。"
        tpl = "green"
    elif text == "":
        content = ("用法:**需求 + 你的想法**,例如「需求 希望增加财报日提醒」。\n\n"
                   "> 内容会匿名写入公开 GitHub，请勿填写客户、账号、密钥等敏感信息。")
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
                   "已写入生效库与留痕库；异常列表会即时标记。金额/比例门禁及 3% 产品结论"
                   "将在下一轮流水线按币种、证券单位与参考价重算，资料不足时仍会保留核验提醒。\n\n"
                   "> 同一标的有多条**值不同**的异常时,请带上日期指定是哪一条,"
                   "例:`确认 AAPL 0.26 2026-08-11`、`确认 XYZ 1:10 2026-09-10`。\n"
                   "> 可在末尾加备注记录你核对了什么,例:`确认 AAPL 0.26 2026-08-11 已比对公司公告`。")
        tpl = "green"
    else:
        content = (f"⚠️ 确认未成功:{msg}\n\n"
                   "用法:`确认 代码 [正确值] [日期] [备注]`,例:`确认 AAPL 0.26 2026-08-11 已比对公司公告`")
        tpl = "red"
    return _card("✅ 人工确认", tpl,
                 [{"tag": "div", "text": {"tag": "lark_md", "content": content}}],
                 site_url, "打开网页面板")


def filing_resolution_card(ok, msg, event_id="", status="", site_url=""):
    """SEC filing 条款核验结案回执。"""
    if ok:
        label = "公司行动" if status == "confirmed" else "普通备案 / 无需操作"
        content = (f"✅ {msg}\n\n结论：**{label}**\n`{event_id}`\n\n"
                   "下一轮流水线会在产品动作判定之前应用本结论，并只推一次状态迁移。")
        template = "green"
    else:
        content = (f"⚠️ {msg}\n\n"
                   "用法：`备案结论 公司行动 EVENT_ID` 或 "
                   "`备案结论 普通备案 EVENT_ID`。\n"
                   "也可使用 `确认备案 EVENT_ID` / `排除备案 EVENT_ID`。")
        template = "red"
    return _card("🏛 SEC 备案结论", template,
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
    """确认/观察留痕；log 已按时间倒序。"""
    title = f"📒 审计留痕 · {ticker}" if ticker else "📒 审计留痕(最近记录)"
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
        et = _etype_label(e)
        dlab = date_label(e.get("etype"))
        if e.get("action") == "watch_forecast":
            head = (f"• {_ago_bj(e.get('at_bj'))}　👁 **{e.get('ticker','')}** {et} "
                    f"预测观察 · {dlab} {e.get('date','') or ''}　_by {who}_")
        elif e.get("action") == "resolve_filing_review":
            result = "公司行动" if e.get("value") == "confirmed" else "普通备案·无需操作"
            head = (f"• {_ago_bj(e.get('at_bj'))}　🏛 **{e.get('ticker','')}** SEC 备案 "
                    f"条款结论 → **{result}**　_by {who}_")
            if e.get("event_id"):
                head += f"\n　ID:`{e['event_id']}`"
        else:
            head = (f"• {_ago_bj(e.get('at_bj'))}　✅ **{e.get('ticker','')}** {et} "
                    f"人工确认 · {dlab} {e.get('date','') or ''} → {vtxt}　_by {who}_")
        sub = []
        if e.get("source"):
            sub.append(f"[核对来源]({e['source']})")
        if e.get("note"):
            sub.append(f"备注:{e['note']}")
        lines.append(head + ("\n　" + " · ".join(sub) if sub else ""))
    body = "\n".join(lines)
    foot = "\n\n_只追加、永不删；历史留痕可能含已退出当前范围的标的。完整表用 `tools/export_ack_log.py` 导 Excel。_"
    return _card(title, "blue",
                 [{"tag": "div", "text": {"tag": "lark_md", "content": body + foot}}],
                 site_url, "打开网页面板")
