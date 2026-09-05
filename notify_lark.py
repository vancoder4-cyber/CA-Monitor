# -*- coding: utf-8 -*-
"""把预警推送到 Lark(飞书国际版)自定义机器人。

用自定义机器人 Webhook(无需建应用):群设置 → 机器人 → 添加自定义机器人 → 拿 Webhook URL,
可选开启「签名校验」拿到 secret。把 URL/secret 配到 .env:
    LARK_WEBHOOK=https://open.larksuite.com/open-apis/bot/v2/hook/xxxx
    LARK_SECRET=（开了签名校验才需要,否则留空)
    LARK_DASHBOARD_URL=（可选,卡片底部"打开面板"按钮指向的地址)
    LARK_NOTIFY_EMPTY=0      # 1=即使没有任何预警也推一条"全部正常"

发送交互卡片:执行催办 / 公司行动条款核验 / 合约门槛核验 / 单源核验 /
新发现 / 冲突 / 空缺，filing 带 SEC 原文链接。
"""
import os
import time
import json
import re
import base64
import hashlib
import hmac
import requests
import config as C
import reconcile as R
from business_time import now as business_now

ETYPE_CN = {"dividend": "分红", "split": "拆股", "filing": "并购/公告"}


def _etype_label(event):
    if isinstance(event, dict):
        return event.get("event_label") or ETYPE_CN.get(event.get("etype"), event.get("etype"))
    return getattr(event, "event_label", "") or ETYPE_CN.get(event.etype, event.etype)


def _date_label(event):
    """事件锚定日的对外名称：分红=除息，拆/合股=生效。"""
    etype = event.get("etype") if isinstance(event, dict) else getattr(event, "etype", None)
    return C.alert_date_label(etype)


def _verification_kind(event):
    """消费 producer 的中央核验类型；兼容尚未刷新字段的旧事件。"""
    if isinstance(event, dict):
        get = event.get
    else:
        get = lambda key, default=None: getattr(event, key, default)
    kind = get("verification_kind")
    if kind in {"forecast", "filing_terms", "contract_threshold"}:
        return kind
    if get("forecast"):
        return "forecast"
    if not get("verification") and get("follow_up_mode") != "verification":
        return ""
    if get("etype") == "filing" and get("filing_relevant") is None:
        return "filing_terms"
    return "contract_threshold"


def _verification_prefix(event):
    return {
        "forecast": "🔎 单源核验 · ",
        "filing_terms": "🔎 公司行动条款核验 · ",
        "contract_threshold": "🔎 合约门槛核验 · ",
    }.get(_verification_kind(event), "")


class LarkDeliveryError(RuntimeError):
    """已配置推送通道但投递失败，调用方必须停止推进去重状态。"""


def _cfg():
    return {
        "webhook": os.environ.get("LARK_WEBHOOK", "").strip(),
        "secret": os.environ.get("LARK_SECRET", "").strip(),
        "dashboard": os.environ.get("LARK_DASHBOARD_URL", "").strip(),
        "notify_empty": os.environ.get("LARK_NOTIFY_EMPTY", "0").strip() == "1",
        "required": os.environ.get("LARK_REQUIRED", "0").strip() == "1",
    }


def _sign(timestamp, secret):
    """Lark 签名:以 '{timestamp}\\n{secret}' 为 HMAC-SHA256 的 key,消息体为空,base64。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _pick(src_fields, field):
    """与网页/交互 Bot 共用多数票 + 源优先级取值，不能再按字典顺序取第一条。"""
    return R.pick_value(src_fields, field)


def _sec_url(g):
    return (g.by_source.get("SEC") or {}).get("url", "")


def _stable_unique(values):
    """稳定去重，确保同一负责人横跨现货/合约时只被 @ 一次。"""
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _load_mentions(env_name="LARK_ALERT_MENTION_OPEN_IDS"):
    """从私密环境变量读取 @ 名单；不得把员工 open_id 放进公开仓库。"""
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return []
    try:
        values = json.loads(raw) if raw.startswith("[") else re.split(r"[,\s]+", raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(values, list):
        return []
    return _stable_unique([
        value for value in (str(item).strip() for item in values)
        if value == "all" or re.fullmatch(r"ou_[A-Za-z0-9]+", value)
    ])


def _event_products(event, *, force_contract=False):
    """读取事件产品归属；旧 payload 缺字段时回退到当前统一资产范围。"""
    if isinstance(event, dict):
        products = event.get("products") or []
        ticker = event.get("ticker", "")
    else:
        products = getattr(event, "products", None) or []
        ticker = getattr(event, "ticker", "")
    if isinstance(products, str):
        products = [products]
    products = {product for product in products if product in {"现货", "合约"}}
    if not products:
        products = set(C.product_tags(ticker))
    if force_contract:
        products.add("合约")
    return products


def _mentions_for_events(events, *, force_contract=False):
    """按事件的现货/合约覆盖面选择负责人，双覆盖时合并并稳定去重。

    两个分组 Secret 尚未配置时，旧的全局 Secret 作为逐组回退，便于无中断
    迁移；待生产完成分组配置后即可移除旧 Secret。
    """
    events = list(events or [])
    if not events:
        return []

    need_spot = False
    need_contract = False
    has_unknown = False
    for event in events:
        products = _event_products(event, force_contract=force_contract)
        need_spot = need_spot or "现货" in products
        need_contract = need_contract or "合约" in products
        has_unknown = has_unknown or not ({"现货", "合约"} & products)

    legacy = _load_mentions()
    mentions = []
    if need_spot:
        mentions.extend(_load_mentions("LARK_ALERT_SPOT_MENTION_OPEN_IDS") or legacy)
    if need_contract:
        mentions.extend(_load_mentions("LARK_ALERT_CONTRACT_MENTION_OPEN_IDS") or legacy)
    if has_unknown:
        mentions.extend(legacy)
    return _stable_unique(mentions)


def _at_tags(open_ids):
    """生成 Lark 卡片 @ 标签:open_id → <at id=ou_xxx></at>;'all' → @所有人。"""
    return "".join(f"<at id={oid}></at>" for oid in open_ids)


def _nasdaq_div(ticker):
    return f"https://www.nasdaq.com/market-activity/{C.public_asset_path(ticker)}/{ticker.lower()}/dividend-history"


def _quick_look(ticker, etype):
    """第三方交叉核对入口：分红优先落到分红历史页，其它事件落到标的总览。"""
    return C.stockanalysis_url(ticker, etype)


def _refs(ticker, etype, g=None, decl_url=None, ir_url=None, references=None):
    """核对链接:消费 run.py 预先生成的统一引用契约，避免不同卡片各自回退。"""
    if g is not None:
        if isinstance(g, dict):
            u = g.get("url") or g.get("sec_url") or g.get("src_url") or ""
            references = references or g.get("references")
            decl_url = decl_url or g.get("decl_url", "")
            ir_url = ir_url or g.get("ir_url", "")
        else:
            u = _sec_url(g)
            references = references or getattr(g, "references", None)
            decl_url = decl_url or getattr(g, "decl_url", "")
            ir_url = ir_url or getattr(g, "ir_url", "")
        if u:
            return f"\n　📄 [SEC原文(本事件)]({u})"
    if etype == "dividend":
        links = references or []
        if links:
            body = " · ".join(f"[{x['label']}]({x['url']})" for x in links if x.get("url"))
            return f"\n　🔗 核对: {body}"
        primary = (f"[SEC·本次宣告 8-K]({decl_url})" if decl_url else
                   f"[官方·IR 分红页]({ir_url})" if ir_url else
                   f"[SEC·公司备案](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&ticker={ticker}&type=&dateb=&owner=include&count=40)")
        return (f"\n　🔗 核对: {primary} · "
                f"[第三方·StockAnalysis（交叉核对，可能滞后）]({_quick_look(ticker, etype)})")
    return ""


def _md_escape(s):
    return str(s).replace("[", "［").replace("]", "］")


def _risk_lines(x):
    if isinstance(x, dict):
        risks = list(x.get("risk") or [])
        contract_action = x.get("contract_action") or {}
    else:
        risks = list(getattr(x, "risk", None) or [])
        contract_action = getattr(x, "contract_action", None) or {}
    # run.py 正常会把中央判定的 message 投影进 risk；这里仍做展示层兜底，
    # 避免旧 payload / 局部更新漏传 risk 后，把「无需操作 / 待核实 / 需操作」
    # 这一条最关键的合约结论静默掉。只补不存在的原文，防止重复展示。
    contract_message = contract_action.get("message")
    if contract_message and contract_message not in risks:
        risks.append(contract_message)
    return "".join(f"\n　⚠️ {risk}" for risk in risks)


def _dates(x):
    """关键日链：宣告、登记、除息/生效/事件日、派发（有哪个显示哪个）。"""
    # filing 的 anchor 可能是 SEC 申报日，也可能是供应商给出的 process/effective
    # date；缺少明确 date_basis 时不能武断写成「生效」。与 Bot 一样用中性事件日。
    lab = _date_label(x)
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


def _ops_copy(x):
    """展示层统一催办文案，并修正旧快照中拆股被写成「除息」的情况。"""
    raw = x.get("ops") or ""
    if not raw:
        return ""
    # 数据/合约核验提醒有独立指令，不改写成执行催办。
    if x.get("forecast") or x.get("verification"):
        return raw
    days = x.get("days")
    if isinstance(days, int):
        return C.alert_copy(days, x.get("etype"))
    label = _date_label(x)
    for old in ("距除息约", "距生效约", "距关键日约", "距事件日约"):
        raw = raw.replace(old, f"距{label}约")
    return raw



def _val(x):
    """金额/比例门禁:有未确认冲突 → 不给确定值,标『待人工确认·勿据此执行』。
    人工「确认」后冲突消解,才会恢复显示确定值。"""
    if x.get("forecast"):
        return " <font color='orange'>🔎单源待核实·勿执行</font>"
    if x.get("disputed") and not x.get("acked"):
        vals = x.get("dispute_vals") or {}
        pairs = " / ".join(f"{v}" for v in dict.fromkeys(vals.values()))
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


def _event_sig(event):
    """跨 dict / EventGroup 的稳定事件签名，优先使用上游事件 ID。

    filing 同一标的同一天可能有多份独立文件；新版 ``event_id`` 含文件指纹，
    不能再退化成 ticker + 类型 + 日期，否则 Lark 会把后到文件误判为重复项。
    """
    if isinstance(event, dict):
        event_id = event.get("event_id")
        if event_id:
            return event_id
        return (event.get("ticker"), event.get("etype"), event.get("date"))
    event_id = getattr(event, "event_id", None)
    if event_id:
        return event_id
    return (
        getattr(event, "ticker", None),
        getattr(event, "etype", None),
        getattr(event, "anchor_date", None),
    )


def _is_routine_filing(event):
    """只过滤中央分类已明确为普通备案的 filing；未知事项继续 fail closed。"""
    if isinstance(event, dict):
        return event.get("etype") == "filing" and event.get("filing_relevant") is False
    return (getattr(event, "etype", None) == "filing" and
            getattr(event, "filing_relevant", None) is False)


def _non_routine(items):
    return [item for item in (items or []) if not _is_routine_filing(item)]


def _visible_alert_items(alerts):
    """按卡片优先级生成互斥的可见事件列表。

    同一公司行动可能同时处于临近催办、状态更新、刚宣告与新发现。推送卡片只
    展示一次，优先级固定为：filing relevance update > round > forecast update
    > contract update > announced > new。filing 状态迁移必须优先，否则同日的
    cadence round 会把「待核实已转确认/普通备案」这一关键信息吞掉。
    """
    claimed = set()

    def take(items, *, include_routine=False):
        visible = []
        for item in items or []:
            # 正常情况下 run.py 已经在入队前过滤；这里是投递边界的最后一道
            # 防线，防止旧缓存或调用方误传把 10-Q/普通 8-K 当公司行动推送。
            if _is_routine_filing(item) and not include_routine:
                continue
            signature = _event_sig(item)
            if signature in claimed:
                continue
            claimed.add(signature)
            visible.append(item)
        return visible

    # 合约已明确 <=3% / 无需操作的事项可以首报、公告或结论更新，但绝不能
    # 因旧缓存/错误调用被包装成「执行催办」。有同事件的新公告或首报时，让它
    # 自然落到后面的低优先级区块并显示中央 no-op 文案。
    actionable_rounds = [
        item for item in alerts.get("rounds", [])
        if not (isinstance(item, dict) and item.get("follow_up_mode") == "none")
    ]

    return {
        # routine resolution 本身是需要投递的一次性结论，不能被普通备案边界
        # 过滤；它只存在于 filing_updates，不会重新进入 CA 日历/执行流。
        "filing_updates": take(alerts.get("filing_updates", []), include_routine=True),
        "rounds": take(actionable_rounds),
        "forecast_updates": take(alerts.get("forecast_updates", [])),
        "contract_updates": take(alerts.get("contract_updates", [])),
        "announced": take(alerts.get("announced", [])),
        "new": take(alerts.get("new", [])),
    }


def _build_card(alerts, meta, dashboard_url=""):
    visible = _visible_alert_items(alerts)
    visible_filing_updates = visible["filing_updates"]
    rounds = visible["rounds"]
    visible_forecast_updates = visible["forecast_updates"]
    visible_contract_updates = visible["contract_updates"]
    visible_announced = visible["announced"]
    visible_new = visible["new"]
    visible_conflicts = _non_routine(alerts.get("conflicts", []))
    visible_gaps = _non_routine(alerts.get("gaps", []))
    n_new = len(visible_new)
    n_verify = sum(1 for x in rounds if x.get("forecast") or x.get("verification"))
    n_round = len(rounds) - n_verify
    n_conf = len(visible_conflicts); n_gap = len(visible_gaps)
    n_forecast = len(_non_routine(alerts.get("forecasts", [])))
    n_forecast_updates = len(visible_forecast_updates)
    n_contract_updates = len(visible_contract_updates)
    n_filing_updates = len(visible_filing_updates)
    n_ann = len(visible_announced)
    # 有冲突/空缺 → 红;有临近/新发现 → 蓝;否则绿
    if n_conf or n_gap:
        template = "red"
    elif (n_round or n_verify or n_ann or n_new or n_forecast_updates or
          n_contract_updates or n_filing_updates):
        template = "blue"
    else:
        template = "green"

    elements = [{
        "tag": "div",
        "text": {"tag": "lark_md",
                 "content": f"📣 新公告 **{n_ann}**　🔔 执行催办 **{n_round}**　🔎 核验提醒 **{n_verify}**　🆕 新发现 **{n_new}**"
                            f"　🔄 条款状态 **{n_filing_updates}**　🔄 预测状态 **{n_forecast_updates}**　🔄 合约结论 **{n_contract_updates}**"
                            f"　❗冲突 **{n_conf}**　🕳 空缺 **{n_gap}**　🔎 预测观察 **{n_forecast}**"}
    }, {"tag": "hr"}]

    formal_rounds = [
        x for x in rounds if not x.get("forecast") and not x.get("verification")
    ]
    required_contract_updates = []
    required_update_sigs = set()
    for x in _non_routine(alerts.get("contract_updates", [])):
        signature = _event_sig(x)
        if x.get("current_status") == "required" and signature not in required_update_sigs:
            required_update_sigs.add(signature)
            required_contract_updates.append(x)
    required_filing_updates = [
        x for x in visible_filing_updates
        if x.get("current_status") == "confirmed"
        and x.get("follow_up_mode") == "execution"
    ]
    # 正式 @ 不只覆盖 cadence round；合约结论刚跨成 required 时即使当前没有
    # 新 round、或明细按全局优先级合并进「预测状态」区，也必须让负责人看到。
    # 所有正式事项合并成一条 @，避免重复提醒。
    _mentions = _stable_unique(
        _mentions_for_events(formal_rounds + required_filing_updates)
        + _mentions_for_events(required_contract_updates, force_contract=True)
    )
    if (formal_rounds or required_contract_updates or required_filing_updates) and _mentions:
        if required_filing_updates:
            kinds = []
            if formal_rounds:
                kinds.append("正式临近催办")
            if required_contract_updates:
                kinds.append("合约需操作结论更新")
            kinds.append("公司行动条款已确认")
            notice = "🔔 有" + "及".join(kinds) + ",请及时处理"
        elif formal_rounds and required_contract_updates:
            notice = "🔔 有正式临近催办及合约需操作结论更新,请及时处理"
        elif required_contract_updates:
            notice = "🔔 有合约结论更新为需操作,请及时处理"
        else:
            notice = "🔔 有正式临近催办事项,请及时处理"
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                        "content": _at_tags(_mentions) + " " + notice}})

    def section(title, lines):
        if not lines:
            return
        body = "\n".join(lines[:30])
        more = f"\n…… 等共 {len(lines)} 条" if len(lines) > 30 else ""
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                        "content": f"**{title}**\n{body}{more}"}})

    def filing_update_line(x):
        prod = ("[" + "+".join(x["products"]) + "] ") if x.get("products") else ""
        kind = x.get("kind")
        if kind == "confirmed":
            conclusion = "✅ 条款核验已确认：已转为正式公司行动"
            if x.get("follow_up_mode") == "execution":
                conclusion += "，请按现货/产品流程及时处理"
        elif kind == "routine":
            conclusion = "✅ 核验完成：普通备案，不属于公司行动；本次无需操作"
        elif kind == "linked":
            conclusion = "🔗 已关联至同日已确认分红，仅作为 SEC 证据；不单独操作"
        elif kind == "expired":
            conclusion = ("⌛ 元数据提示超过核验期仍无证据，已停止每日提醒；"
                          "事项仍未核实，不得据此判断无需操作或执行")
        else:
            overdue = abs(x.get("days") or 0)
            conclusion = f"🔎 条款仍待核实：事件日已过 {overdue} 天；核实前勿执行"
        note = f"\n　📝 {_md_escape(x.get('note'))}" if x.get("note") else ""
        return (f"• {prod}**{x['ticker']}** {_etype_label(x)} — {conclusion}；"
                f"{_date_label(x)} {x.get('date')}{note}"
                + _risk_lines(x)
                + _refs(x["ticker"], x.get("etype"), g=x,
                        references=x.get("references")))

    section("🔄 公司行动条款状态更新", [
        filing_update_line(x) for x in visible_filing_updates
    ])

    def forecast_update_line(x):
        kind = x.get("kind")
        prod = ("[" + "+".join(x["products"]) + "] ") if x.get("products") else ""
        label = _etype_label(x)
        if kind in ("promoted", "declared"):
            why = ("已核验公司官方宣告" if x.get("official") else "已获取宣告日") \
                if kind == "declared" else "已获第二个独立源确认"
            return (f"• ✅ {prod}**{x['ticker']}** {label} 预测已转正式 — "
                    f"{_date_label(x)} {x.get('date')}；{why}" + _risk_lines(x))
        if kind == "updated":
            old_date = x.get("previous_date") or "—"
            old_amt = x.get("previous_amount")
            value = x.get("amount") if x.get("amount") is not None else x.get("ratio")
            return (f"• 🔄 {prod}**{x['ticker']}** {label} 预测更新 — "
                    f"{_date_label(x)} {old_date} → {x.get('date')}；值 {old_amt if old_amt is not None else '—'} → {value if value is not None else '—'}")
        return (f"• ❌ {prod}**{x['ticker']}** {label} 预测失效 — "
                f"预计{_date_label(x)} {x.get('date')} 已过仍未获公司宣告或独立源确认；不执行。")

    section("🔄 预测状态更新(自动追踪)",
            [forecast_update_line(x) + _refs(x["ticker"], x.get("etype"), g=x, references=x.get("references"))
             for x in visible_forecast_updates])

    def contract_update_line(x):
        labels = {
            "required": "现已达到 >3% 合约操作门槛",
            "not_required": "已确认合约本次无需操作",
            "review": "转为合约门槛待核实",
        }
        current = x.get("current_status")
        return (f"• 🔄 **{x['ticker']}** {_etype_label(x)} "
                f"{_date_label(x)} {x.get('date')} — {labels.get(current, current or '结论更新')}" + _risk_lines(x))

    section("🔄 合约操作结论更新",
            [contract_update_line(x) for x in visible_contract_updates])

    # 🙋 待人工确认(不豁免:挂着就一直报,只有「确认」能消解)—— 超期则 @ 人升级
    _rv = alerts.get("review") or {}
    _esc = _rv.get("escalate_days", 3)
    # review summary 是上游快照；展示和 @ 必须以本卡实际保留的异常项重算，
    # 否则被 routine-filing 防线过滤的旧项仍可能留下幽灵计数/误 @。
    _review_items = visible_conflicts + visible_gaps
    _review_ages = [getattr(item, "age_days", 0) or 0 for item in _review_items]
    _review_open = n_conf + n_gap
    _review_overdue = sum(1 for age in _review_ages if age >= _esc)
    _review_max_age = max(_review_ages) if _review_ages else 0
    if _review_open:
        _overdue_items = [
            item for item in _review_items
            if (getattr(item, "age_days", 0) or 0) >= _esc
        ]
        _m = _mentions_for_events(_overdue_items)
        head = (f"🙋 **待人工确认 {_review_open} 条**"
                f"(冲突 {n_conf} · 空缺 {n_gap})"
                f"　最久已挂 **{_review_max_age} 天**")
        if _review_overdue and _m:
            head = (_at_tags(_m) + f" ❗ 有 **{_review_overdue}** 条异常超过 {_esc} 天没人确认,请尽快处理\n" + head)
        head += "\n　👉 核对后在群里发 **确认 代码 [正确值]**(例:`确认 AAPL 0.26`)即可消解;不确认会一直报。"
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": head}})
        elements.append({"tag": "hr"})

    # 🔔 临近提醒：命中产品动作条件才是执行催办；单源或合约门槛不明
    # 只要求数据核验。两类均按 ≤14 天每天推、进入 30 天窗口知会一次。
    rl = []
    for x in rounds:
        dates = _dates(x)
        val = _val(x)
        prod = ("[" + "+".join(x["products"]) + "] ") if x.get("products") else ""
        verification_kind = _verification_kind(x)
        is_forecast = verification_kind == "forecast"
        is_verification = bool(verification_kind)
        prefix = ({
            "forecast": "🔎 单源核验 · ",
            "filing_terms": "🔎 公司行动条款核验 · ",
            "contract_threshold": "🔎 合约门槛核验 · ",
        }.get(verification_kind) or
                  ("✅ 单源已转正式 · " if x.get("promoted_from_forecast") else ""))
        color = "orange" if is_verification else "red"
        line = (f"• {prefix}{prod}**{x['ticker']}** {_etype_label(x)}{val} — "
                f"<font color='{color}'>D-{x['days']}</font>　{dates}")
        if is_forecast:
            srcs = ", ".join(x.get("srcs") or []) or "未知"
            line += f"\n　📡 单一数据源：{srcs}"
        ops = _ops_copy(x)
        if ops:
            line += f"\n　👉 {ops}"
        line += _risk_lines(x)
        line += _refs(x["ticker"], x["etype"], g=x, decl_url=x.get("decl_url"), ir_url=x.get("ir_url"),
                      references=x.get("references"))
        rl.append(line)
    section("🔔 临近提醒(执行催办 + 单源/公司行动条款/合约门槛核验；非本周清单：≤14天每天 · 30天知会)", rl)

    # 📣 新公告:全局互斥后的可见项
    al = []
    for x in visible_announced:
        prod = ("[" + "+".join(x["products"]) + "] ") if x.get("products") else ""
        val = _val(x)
        days = f" · <font color='red'>还剩 {x['days']} 天</font>" if x.get("days") is not None else ""
        prefix = "✅ 预测已转正式 · " if x.get("forecast_watch") else ""
        prefix += _verification_prefix(x)
        line = (f"• {prefix}{prod}**{x['ticker']}** {_etype_label(x)}{val} —— "
                f"宣告 {x.get('decl')} · {_date_label(x)} {x['date']}{days}")
        al.append(line + _risk_lines(x) + _refs(x["ticker"], x["etype"], g=x,
                                                references=x.get("references")))
    section("📣 新公告(刚宣告)", al)
    # 「待执行」区已并入上面的「临近催办」,不再单列。

    # 字段冲突(零容忍:不豁免,每次都报,直到人工「确认」)
    def _aged(g):
        a = getattr(g, "age_days", 0) or 0
        if a >= _esc:
            return f"　<font color='red'>⏳已挂 {a} 天未确认</font>"
        return f"　<font color='grey'>已挂 {a} 天</font>" if a else ""
    def _adr(g):  # ADR 预扣税提示:保证认税前毛额
        try:
            import reconcile as _R
            n = _R.adr_tax_note(g.ticker, g.by_source) if g.etype == "dividend" else ""
        except Exception:
            n = ""
        return f"\n　<font color='red'>{n}</font>" if n else ""
    cl = [f"• **{g.ticker}** {_etype_label(g)} {_date_label(g)} {g.anchor_date}:"
          f" {_md_escape('; '.join(g.conflicts))}{_aged(g)}{_adr(g)}{_risk_lines(g)}{_refs(g.ticker, g.etype, g)}"
          for g in visible_conflicts]
    section("❗ 字段冲突(零容忍 · 需人工确认)", cl)

    # 数据空缺(同样需人工确认)
    gl = [f"• **{g.ticker}** {_etype_label(g)} {_date_label(g)} {g.anchor_date}{_aged(g)}:"
          f" {_md_escape('; '.join(g.gaps))}" for g in visible_gaps]
    section("🕳 数据空缺", gl)

    # 新发现
    nl = []
    for g in visible_new:
        if R.is_disputed(g):
            vals = [v.get("amount") if v.get("amount") is not None else v.get("ratio")
                    for v in g.by_source.values()]
            vals = [str(v) for v in dict.fromkeys(v for v in vals if v is not None)]
            val = f" <font color='red'>⚠️各源不一致({' / '.join(vals)})·待确认，勿执行</font>"
        else:
            display = getattr(g, "value_display", "")
            if display and not getattr(g, "value_verified", False):
                candidate = getattr(g, "selected_amount", None)
                if candidate is None:
                    candidate = getattr(g, "selected_ratio", None)
                val = (" <font color='orange'>⚠️数值未交叉验证(" +
                       _md_escape(candidate) + ")·待确认，勿执行</font>")
            elif display:
                val = " " + display
            else:
                amt = getattr(g, "ack_value", None)
                if amt is None:
                    amt = _pick(g.by_source, "amount")
                ratio = _pick(g.by_source, "ratio")
                val = (f" ${amt}" if amt is not None else "") + (f" {ratio}" if ratio else "")
        products = C.product_tags(g.ticker)
        prod = ("[" + "+".join(products) + "] ") if products else ""
        line = (f"• {_verification_prefix(g)}{prod}**{g.ticker}** {_etype_label(g)} "
                f"{_date_label(g)} {g.anchor_date}{val}")
        if g.etype == "filing":
            line += f" {_md_escape(g.note or '')}"
            u = _sec_url(g)
            if u:
                line += f" [SEC原文]({u})"
        if g.etype == "dividend":
            line += _refs(g.ticker, g.etype, g)
        line += _risk_lines(g)
        nl.append(line)
    section("🆕 新发现事件", nl)

    if dashboard_url:
        elements.append({"tag": "hr"})
        elements.append({"tag": "action", "actions": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "打开公司行动面板"},
            "url": dashboard_url, "type": "primary"}]})

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": template,
                       "title": {"tag": "plain_text",
                                 "content": f"📣 公司行动预警 · {meta['generated']}"}},
            "elements": elements,
        }
    }


def notify(alerts, meta):
    """根据 .env 配置推送到 Lark。

    本地未配置和无预警静默是合法跳过；已配置通道后的任何投递失败都抛出
    LarkDeliveryError，让定时任务变红并保留上一份去重状态供下次重试。
    """
    cfg = _cfg()
    if not cfg["webhook"]:
        if cfg["required"]:
            raise LarkDeliveryError("LARK_REQUIRED=1 但未配置 LARK_WEBHOOK")
        return False, "未配置 LARK_WEBHOOK,跳过推送"

    # pending / forecasts 是网页和 Bot 的完整清单；推送数量必须与卡片实际可见项
    # 使用同一套全局互斥结果，不能把被高优先级区块吸收的重复事件重复计数。
    visible = _visible_alert_items(alerts)
    total = (sum(len(items) for items in visible.values())
             + len(_non_routine(alerts.get("conflicts", [])))
             + len(_non_routine(alerts.get("gaps", []))))
    if total == 0 and not cfg["notify_empty"]:
        return False, "无预警内容,跳过(设 LARK_NOTIFY_EMPTY=1 可强制推送)"

    payload = _build_card(alerts, meta, cfg["dashboard"])
    if cfg["secret"]:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = _sign(ts, cfg["secret"])

    try:
        r = requests.post(cfg["webhook"], json=payload, timeout=15)
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        code = j.get("code", j.get("StatusCode", None))
        if r.status_code == 200 and type(code) is int and code == 0:
            return True, f"已推送 {total} 条预警到 Lark"
        raise LarkDeliveryError(f"Lark 返回异常: HTTP {r.status_code} {r.text[:160]}")
    except LarkDeliveryError:
        raise
    except Exception as e:
        raise LarkDeliveryError(f"推送失败: {e}") from e


if __name__ == "__main__":
    # 自检:发一条测试卡片
    fake = {"new": [], "rounds": [], "conflicts": [], "gaps": []}
    meta = {"generated": business_now().strftime("%Y-%m-%d %H:%M ET")}
    os.environ.setdefault("LARK_NOTIFY_EMPTY", "1")
    print(notify(fake, meta))
