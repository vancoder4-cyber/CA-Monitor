# -*- coding: utf-8 -*-
"""主流程:抓取 → 核对 → 报警 → 输出面板+digest。

两段式(绕过单次运行时限,也便于调度):
    python run.py fetch [T1 T2 ...]   # 抓取+核对指定票(默认全量),结果缓存到 data/cache/
    python run.py build               # 合并所有缓存 → 计算报警 → 写 dashboard.html + digest
    python run.py                     # = fetch 全量 + build(一次跑完,适合定时任务)

状态文件 data/state.json:已见事件签名(新发现判定)+ 已触发预警轮次(去重)。
"""
import os, sys, json, re, datetime as dt
import requests
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import config as C
import contract_policy as CP
import sources as S
import reconcile as R
import report as RP
import notify_lark
from business_time import now as business_now, today as business_today

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")


def _now_label():
    """带时区标注的生成时间:美东(ET) + 北京。GitHub 服务器是 UTC,直接 now() 会显示 UTC 造成误解。"""
    et = business_now()
    bj = et.astimezone(dt.timezone(dt.timedelta(hours=8)))
    return f"{et.strftime('%Y-%m-%d %H:%M')} ET / {bj.strftime('%H:%M')} 北京"
CACHE = os.path.join(DATA, "cache")
os.makedirs(CACHE, exist_ok=True)
STATE_PATH = os.path.join(DATA, "state.json")
REFERENCE_PRICE_PATH = os.path.join(DATA, "reference_prices.json")
FORECAST_WATCH_PATH = os.path.join(DATA, "forecast_watch.json")
OUT_HTML = os.path.join(HERE, "dashboard.html")
OUT_DIGEST = os.path.join(DATA, "latest_digest.txt")
OUT_SITEDATA = os.path.join(HERE, "site_data.json")  # 供交互机器人读取(会发布到 Pages/data.json)


def load_changelog():
    """解析 CHANGELOG.md -> [{head, items:[...]}, ...](最新在前)。"""
    path = os.path.join(HERE, "CHANGELOG.md")
    if not os.path.exists(path):
        return []
    entries, cur = [], None
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.rstrip()
            if s.startswith("## "):
                if cur:
                    entries.append(cur)
                cur = {"head": s[3:].strip(), "items": []}
            elif s.startswith("- ") and cur is not None:
                cur["items"].append(s[2:].strip())
    if cur:
        entries.append(cur)
    return entries


def load_acknowledged():
    """读取人工确认 data/acknowledged.json -> [{ticker, value, etype, date, by, at}, ...]。"""
    path = os.path.join(DATA, "acknowledged.json")
    if not os.path.exists(path):
        return []
    try:
        data = json.load(open(path, encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def load_forecast_watches():
    """读取人工标记的预测观察项。

    观察不是确认：它保留抓取和后续升级能力，只禁止在未获证实时进入执行催办。
    """
    if not os.path.exists(FORECAST_WATCH_PATH):
        return []
    try:
        data = json.load(open(FORECAST_WATCH_PATH, encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def forecast_key(watch):
    return f"{watch.get('ticker')}|{watch.get('etype')}|{watch.get('date')}"


def match_forecast_watch(watches, ticker, etype, date, window_days=14, statuses=("watching",)):
    """按标的/类型匹配观察项，允许供应商小幅改期，避免新日期漏掉旧预测。"""
    try:
        event_day = dt.date.fromisoformat(date)
    except Exception:
        return None
    candidates = []
    for w in watches:
        if (w.get("status", "watching") not in statuses or w.get("ticker") != ticker
                or w.get("etype") != etype):
            continue
        try:
            distance = abs((event_day - dt.date.fromisoformat(w.get("date", ""))).days)
        except Exception:
            continue
        if distance <= window_days:
            candidates.append((distance, w))
    return min(candidates, key=lambda x: x[0])[1] if candidates else None


def schedule_event_reminder(event, signature, fired, today):
    """按统一 30/14 天节奏生成一次临近提醒。

    `forecast=True` 的单源事件和合约门槛 review 同样会提醒，但只生成核验任务，
    绝不冒充执行指令。`fired` 由调用方持久化，用于保证
    30 天窗口只知会一次、14 天内每天最多一次。
    """
    days = event.get("days")
    if not isinstance(days, int) or days < 0:
        return None

    # 核验提醒、现货动作和合约门槛动作使用独立去重轨道。现金分红的估算
    # 从 ≤3% 跨到 >3% 时，不能被此前的普通提醒静默压掉。
    verification = bool(event.get("forecast") or event.get("follow_up_mode") == "verification")
    if event.get("forecast"):
        state_key = f"{signature}#verification"
    elif event.get("reminder_state_suffix"):
        state_key = f"{signature}#{event['reminder_state_suffix']}"
    else:
        state_key = signature
    previous = fired.get(state_key)
    state = dict(previous) if isinstance(previous, dict) else {}
    due = None
    if days <= C.ALERT_DAILY_WITHIN:
        if state.get("last_daily") != today:
            due = "daily"
            state["last_daily"] = today
    elif days <= C.ALERT_HEADSUP_DAY:
        if not state.get("headsup"):
            due = "headsup"
            state["headsup"] = True
    fired[state_key] = state
    if not due:
        return None

    reminder = {**event, "round": days, "cadence": due}
    if event.get("forecast"):
        reminder.update({
            "ops": "🔎 单源待核实：请核对公司官方公告或第二个独立源；未确认前勿执行。",
            "risk_copy": "数据核验提醒，不是公司行动执行指令。",
            "risk": [],
            "verification": True,
        })
    elif verification:
        reminder.update({
            "ops": "🔎 合约门槛待核实：请补齐可靠金额/比例或参考价；确认影响严格超过 3% 前不执行合约调整。",
            "risk_copy": "合约门槛核验提醒，不是执行指令。",
            "verification": True,
        })
    else:
        reminder.update({
            "ops": C.alert_copy(days),
            "risk_copy": C.ROUND_RISK_TBD,
            "verification": False,
        })
    return reminder


def load_refs():
    """读取参考链接维护台 refs.json(每标的 IR 分红页等)。"""
    p = os.path.join(HERE, "refs.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


_FILING_OVERRIDES = None


def filing_overrides():
    """refs.json 里人工核实过的『具体宣告 filing』:key = 代码|除息日 → 直达 URL。"""
    global _FILING_OVERRIDES
    if _FILING_OVERRIDES is None:
        _FILING_OVERRIDES = load_refs().get("filing_overrides", {}) or {}
    return _FILING_OVERRIDES


def _ack_match(acks, ticker, etype, date):
    """只接受同标的+同类型+同日期的确认；旧宽键不得误放行另一种公司行动。"""
    for a in acks:
        if (a.get("ticker") == ticker and a.get("etype") == etype
                and a.get("date") == date):
            return a
    return None


def build_sec8k_index(all_groups):
    """每个标的的 8-K 索引:ticker -> [(filing_date, url, items), ...]。"""
    idx = {}
    for tk, groups in all_groups.items():
        for g in groups:
            if g.etype == "filing" and (g.note or "").startswith("8-K"):
                sec = g.by_source.get("SEC") or {}
                if sec.get("url"):
                    idx.setdefault(tk, []).append((g.anchor_date, sec.get("url", ""), sec.get("items", "")))
    return idx


def match_decl_8k(idx, ticker, decl_date):
    """匹配该标的的『宣告分红 8-K』:仅认 Item 8.01(宣告分红的标准载体),窗口 ±3 天取最近。
    用元数据(item 代码)判定,不抓正文——既根治误挂(如投票结果 Item 5.07 被排除),又不依赖网络。
    匹配不到返回 ''(前端再选公司 IR / SEC 公司备案)。宁可少挂,也不挂错。"""
    if not decl_date:
        return ""
    try:
        D = dt.date.fromisoformat(decl_date)
    except Exception:
        return ""
    best = None  # (distance, url)
    for d, url, items in idx.get(ticker, []):
        if not url or "8.01" not in (items or ""):   # 必须含 Item 8.01
            continue
        try:
            dist = abs((dt.date.fromisoformat(d) - D).days)
        except Exception:
            continue
        if dist > 3:
            continue
        if best is None or dist < best[0]:
            best = (dist, url)
    return best[1] if best else ""


def _event_key(ticker, etype, date):
    return f"{ticker}|{etype}|{date}"


def _stockanalysis_url(ticker, etype):
    return C.stockanalysis_url(ticker, etype)


def _sec_company_url(ticker):
    return ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
            f"&ticker={ticker}&type=&dateb=&owner=include&count=40")


def apply_official_event_overrides(all_groups, refs=None, *, allowed_tickers=None):
    """合入人工逐项核验的公司官方事件覆盖层。

    某些供应商会先给出下一次分红日期，却迟迟没有 declaration date。只有明确登记在
    refs.json 的官方公告才能补这一空白；覆盖仍走 reconcile 的零容忍规则，若采集源
    与官方值冲突会正常报警，不会悄悄替换。
    """
    refs = refs if isinstance(refs, dict) else load_refs()
    # 生产默认只允许当前可监控证券。这样 refs.json 的历史条目不会因为 setdefault()
    # 重新注入已移除标的；可在独立回归 fixture 显式传入 scope 测试旧事件。
    allowed_tickers = set(C.TICKERS if allowed_tickers is None else allowed_tickers)
    overrides = refs.get("official_event_overrides", {}) or {}
    for key, raw in overrides.items():
        if not isinstance(raw, dict):
            continue
        try:
            ticker, etype, anchor = key.split("|", 2)
        except ValueError:
            print(f"[refs] 跳过格式错误的 official_event_overrides key: {key}")
            continue
        if ticker not in allowed_tickers:
            print(f"[refs] 跳过非当前监控范围的 official_event_overrides: {key}")
            continue
        if not raw.get("url"):
            print(f"[refs] 跳过缺官方 URL 的 official_event_overrides: {key}")
            continue
        groups = all_groups.setdefault(ticker, [])
        g = next((x for x in groups if x.etype == etype and x.anchor_date == anchor), None)
        if g is None:
            g = R.EventGroup(ticker=ticker, etype=etype, anchor_date=anchor)
            groups.append(g)
        fields = {"ex_date": anchor, "url": raw["url"], "verified_at": raw.get("verified_at", ""),
                  "label": raw.get("label", "官方公告/IR")}
        for fld in ("declaration_date", "record_date", "pay_date", "amount", "ratio",
                    "subtype", "amount_currency", "amount_unit"):
            if raw.get(fld) is not None:
                fields[fld] = raw[fld]
        g.by_source["CompanyIR"] = fields
        g.sources_ok = sorted(set(g.sources_ok) | {"CompanyIR"})
        R.evaluate_group(g)
    for groups in all_groups.values():
        groups.sort(key=lambda g: (g.anchor_date or ""), reverse=True)


def active_forecast_watches(watches, *, allowed_tickers=None):
    """仅保留当前可监控证券的人工预测观察项。

    forecast_watch.json 与审计日志会保留历史记录，但历史/商品标的不能重新生成
    预测失效、升级或推送。独立测试可显式传入 scope。
    """
    allowed_tickers = set(C.TICKERS if allowed_tickers is None else allowed_tickers)
    return [w for w in (watches or [])
            if isinstance(w, dict) and w.get("ticker") in allowed_tickers]


def _dividend_references(ticker, date, decl, refs, sec8k):
    """返回同一份引用契约，供网页、推送和交互 Bot 共用。

    第三方仅用于交叉核对，明确标注可能滞后；正式化永远以官方公告/IR 或 SEC 为准。
    """
    refs = refs or {}
    override = (refs.get("official_event_overrides", {}) or {}).get(
        _event_key(ticker, "dividend", date), {}) or {}
    ir_map = refs.get("ir_dividend", {}) or {}
    filing_url = (refs.get("filing_overrides", {}) or {}).get(f"{ticker}|{date}", "")
    exact_8k = filing_url or match_decl_8k(sec8k, ticker, decl)
    rows, seen = [], set()

    def add(label, url, kind):
        if url and url not in seen:
            rows.append({"label": label, "url": url, "kind": kind})
            seen.add(url)

    if override.get("url"):
        add(override.get("label") or "官方·本次公告/IR", override["url"], "official_event")
    if exact_8k:
        add("SEC·本次宣告 8-K", exact_8k, "official_filing")
    ir_url = ir_map.get(ticker, "")
    if ir_url:
        add("官方·IR 分红页", ir_url, "official_ir")
    if not rows:
        add("SEC·公司备案", _sec_company_url(ticker), "official_company")
    add("第三方·StockAnalysis（交叉核对，可能滞后）", _stockanalysis_url(ticker, "dividend"), "third_party")
    return rows


def attach_event_references(target, refs, sec8k):
    """给 dict 或 EventGroup 预先写入统一引用字段，渲染器不得各自猜回退链接。"""
    if isinstance(target, dict):
        etype = target.get("etype")
        ticker = target.get("ticker", "")
        date = target.get("date", "")
        decl = target.get("decl")
    else:
        etype = getattr(target, "etype", "")
        ticker = getattr(target, "ticker", "")
        date = getattr(target, "anchor_date", "")
        decl = R.pick_value(getattr(target, "by_source", {}), "declaration_date")
    if etype != "dividend":
        return
    links = _dividend_references(ticker, date, decl, refs, sec8k)
    primary = next((x for x in links if x["kind"] != "third_party"), None)
    third = next((x for x in links if x["kind"] == "third_party"), None)
    fields = {
        "references": links,
        "primary_url": (primary or {}).get("url", ""),
        "primary_label": (primary or {}).get("label", ""),
        "third_party_url": (third or {}).get("url", ""),
        # 兼容旧版 Railway Bot：它会把 decl_url 标为「宣告 8-K」，因此这里只能放
        # 真正的 SEC 本次 filing；官方事件/IR 则落到 ir_url，避免链接正确但标签误导。
        "decl_url": next((x["url"] for x in links if x["kind"] == "official_filing"), ""),
        "ir_url": next((x["url"] for x in links
                        if x["kind"] in ("official_ir", "official_event")), ""),
    }
    if isinstance(target, dict):
        target.update(fields)
    else:
        for k, v in fields.items():
            setattr(target, k, v)


def _product_fields(g):
    return {
        "contract_action": getattr(g, "contract_action", {}),
        "follow_up_mode": getattr(g, "follow_up_mode", "execution"),
        "reminder_state_suffix": getattr(g, "reminder_state_suffix", ""),
        "risk": getattr(g, "risk", []),
    }


def _event_value_fields(g):
    amount = getattr(g, "selected_amount", None)
    ratio = getattr(g, "selected_ratio", None)
    subtype = getattr(g, "action_subtype", "")
    currency = getattr(g, "selected_amount_currency", "")
    unit = getattr(g, "selected_amount_unit", "")
    return {
        "amount": amount,
        "ratio": ratio,
        "subtype": subtype,
        "event_label": CP.event_label(g.etype, subtype),
        "amount_currency": currency,
        "amount_unit": unit,
        "value_display": CP.value_display(
            g.etype,
            amount=amount,
            ratio=ratio,
            subtype=subtype,
            amount_currency=currency,
            amount_unit=unit,
        ),
        "value_verified": bool(getattr(g, "value_verified", False)),
    }


def attach_product_action(g, reference_price, today, forecast=False):
    """给事件组生成一次产品动作结论，后续所有展示面只复制这个结果。"""
    value_field = "ratio" if g.etype == "split" else "amount"
    official_fields = (g.by_source or {}).get("CompanyIR") or {}
    ack_value_verified = bool(
        getattr(g, "ack_exact", False) and getattr(g, "ack_value", None) is not None
    )
    value_verified = (
        g.etype == "filing" or ack_value_verified or
        official_fields.get(value_field) is not None or R.n_src(g.by_source, value_field) >= 2
    )
    amount = getattr(g, "ack_value", None) if ack_value_verified else None
    if amount is None and g.etype == "dividend":
        adr = R.adr_tax(g.ticker, g.by_source)
        amount = adr.get("gross") if adr else R.pick_value(g.by_source, "amount")
    elif amount is None:
        amount = R.pick_value(g.by_source, "amount")
    ratio = (S.normalize_ratio(getattr(g, "ack_value", None))
             if ack_value_verified and g.etype == "split"
             else R.pick_value(g.by_source, "ratio"))

    # 金额和美股参考价必须是同一上市证券单位。美股数据供应商均标成
    # USD/listed_security；若是公司 IR 本币普通股口径（TSM 等 ADR）则 fail closed。
    # 人工确认只确认“数值”，不能凭空证明币种和证券单位。单位必须从与确认值
    # 一致的来源继承；找不到可验证口径时保持为空，让 policy fail closed。
    amount_currency = ""
    amount_unit = ""
    if amount is not None:
        support = []
        for fields in (g.by_source or {}).values():
            try:
                same = fields.get("amount") is not None and abs(float(fields["amount"]) - float(amount)) < 0.0005
            except (TypeError, ValueError):
                same = False
            if same:
                support.append(fields)
        currencies = {str(x.get("amount_currency") or "").upper() for x in support}
        units = {x.get("amount_unit") or "" for x in support}
        if len(currencies) == 1:
            amount_currency = next(iter(currencies))
        if len(units) == 1:
            amount_unit = next(iter(units))

    note = (g.note or "").lower()
    relevance = [x.get("relevant") for x in (g.by_source or {}).values()]
    keyword_relevant = any(token in note for token in (
            "并购", "退市", "分拆", "证券变更", "要约", "merger", "spin_off",
            "spin-off", "name_change", "symbol_change", "redemption", "worthless",
        ))
    if any(value is True for value in relevance) or keyword_relevant:
        filing_relevant = True
    elif relevance and all(value is False for value in relevance):
        filing_relevant = False
    else:
        # Alpaca / FINX “其它公司行动”没有足够条款时必须 review，不能当普通备案 no-op。
        filing_relevant = None
    subtype = CP.action_subtype(g.by_source, g.etype)
    g.selected_amount = amount
    g.selected_ratio = ratio
    g.action_subtype = subtype
    g.event_label = CP.event_label(g.etype, subtype)
    g.selected_amount_currency = amount_currency
    g.selected_amount_unit = amount_unit
    g.value_display = CP.value_display(
        g.etype,
        amount=amount,
        ratio=ratio,
        subtype=subtype,
        amount_currency=amount_currency,
        amount_unit=amount_unit,
    )
    decision = CP.evaluate(
        g.ticker,
        g.etype,
        amount=amount,
        ratio=ratio,
        subtype=subtype,
        reference_price=reference_price,
        amount_currency=amount_currency,
        amount_unit=amount_unit,
        value_verified=value_verified,
        forecast=forecast,
        disputed=bool(g.conflicts) and not ack_value_verified,
        filing_relevant=filing_relevant,
        today=today,
    )
    g.value_verified = bool(value_verified and not (g.conflicts and not ack_value_verified))
    mode = CP.follow_up_mode(g.ticker, decision)
    g.contract_action = decision
    g.follow_up_mode = mode
    g.reminder_state_suffix = CP.reminder_state_suffix(g.ticker, decision, mode)
    g.risk = C.risk_note(g.ticker, g.etype, decision)
    return decision


def _grp_brief(g):
    u = (g.by_source.get("SEC") or {}).get("url", "") if g.etype == "filing" else ""
    values = _event_value_fields(g)
    if R.is_disputed(g):
        values.update({"amount": None, "ratio": None, "value_display": ""})
    # src_url:①并购/退市→SEC 源的真实 filing url;②分红/拆股→refs.json 里**人工核实过**的
    # filing_overrides(代码|除息日)。不再用 EFTS 全文猜(会命中章程/发债8-K/港交所月报)。
    src = u or filing_overrides().get(f"{g.ticker}|{g.anchor_date}", "")
    # ADR 分红:识别毛额/净额,附提示,保证运营认税前毛额(见 config.ADR_WHT)
    _adr = R.adr_tax(g.ticker, g.by_source) if g.etype == "dividend" else None
    return {"ticker": g.ticker, "etype": g.etype, "date": g.anchor_date,
            "note": g.note, **values, "sec_url": u, "src_url": src,
            "references": getattr(g, "references", []),
            "primary_url": getattr(g, "primary_url", ""),
            "primary_label": getattr(g, "primary_label", ""),
            "third_party_url": getattr(g, "third_party_url", ""),
            "srcs": sorted(g.by_source.keys()),
            "amt_srcs": max(R.n_src(g.by_source, "amount"), R.n_src(g.by_source, "ratio")),
            "official": R.has_official_source(g.by_source),
            "adr_note": (R.adr_tax_note(g.ticker, g.by_source) if g.etype == "dividend" else ""),
            "adr_gross": (_adr["gross"] if _adr else None),
            "conflicts": g.conflicts, "gaps": g.gaps,
            **_product_fields(g)}


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
            st.setdefault("seen", {})
            st.setdefault("fired_rounds", {})
            st.setdefault("declared", {})   # sig -> 已推送过的宣告日
            st.setdefault("forecast_status", {})  # 自动单源 + 人工观察的状态，供升级/改期/失效通知去重
            st.setdefault("contract_action_status", {})  # sig -> 合约门槛上次结论
            return st
    return {"seen": {}, "fired_rounds": {}, "declared": {}, "forecast_status": {},
            "contract_action_status": {}}


def save_state(st):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def load_reference_prices():
    """读取跨运行 last-known-good 行情；过期由 contract_policy 判为 review。"""
    try:
        with open(REFERENCE_PRICE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_reference_prices(prices):
    with open(REFERENCE_PRICE_PATH, "w", encoding="utf-8") as f:
        json.dump(prices, f, ensure_ascii=False, indent=2)


def deliver_then_save(alerts, meta, state):
    """Lark 成功投递或合法跳过后才推进去重状态。"""
    sent, info = notify_lark.notify(alerts, meta)
    print(f"Lark: {info}")
    save_state(state)
    return sent, info


def sig(g):
    return f"{g.ticker}|{g.etype}|{g.anchor_date}"


# ---------------- FETCH ----------------
def _fetch_one(tk, keys, av_on):
    results = S.fetch_all_for_ticker(tk, keys, av_enabled=av_on)
    health = {}
    for r in results:
        if health.get(r.source) == "unavailable":
            continue
        health[r.source] = r.status
    groups = R.reconcile_ticker(results)
    payload = {"ticker": tk, "fetched": dt.datetime.now().isoformat(timespec="seconds"),
               "health": health, "reference_price": S.reference_price(tk),
               "groups": [g.to_dict() for g in groups]}
    with open(os.path.join(CACHE, f"{tk}.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return tk, len(groups), health


def fetch(tickers, workers=8, av_limit=24):
    tickers = list(dict.fromkeys(tickers))
    keys = C.get_keys()
    S.replace_reference_prices(load_reference_prices())
    S.prefetch_nasdaq_splits()
    S.prefetch_alpaca(C.TICKERS, keys.get("ALPACA_KEY_ID"), keys.get("ALPACA_SECRET"))
    # Alpha Vantage 免费 25/天:只给前 av_limit 支启用,其余跳过(避免限流+提速)
    av_set = set(tickers[:av_limit])
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_one, tk, keys, tk in av_set): tk for tk in tickers}
        for fu in as_completed(futs):
            tk, n, health = fu.result()
            done += 1
            print(f"[{done}/{len(tickers)}] {tk}: {n} 组 | " +
                  ", ".join(f"{s}:{st}" for s, st in health.items()))
    save_reference_prices(S.all_reference_prices())


# ---------------- BUILD ----------------
def build():
    all_groups, source_health, reference_prices = {}, {}, {}
    for tk in C.TICKERS:
        p = os.path.join(CACHE, f"{tk}.json")
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        all_groups[tk] = [R.EventGroup.from_dict(x) for x in d["groups"]]
        source_health[tk] = d["health"]
        if isinstance(d.get("reference_price"), dict):
            reference_prices[tk] = d["reference_price"]

    # 官方逐项核验覆盖层必须在所有判定之前合入：它既补足官方 declaration date，
    # 又继续参与零容忍冲突检测，不能只在渲染层偷偷换链接。
    refs_config = load_refs()
    apply_official_event_overrides(all_groups, refs_config)
    sec8k_index = build_sec8k_index(all_groups)
    for groups in all_groups.values():
        for g in groups:
            attach_event_references(g, refs_config, sec8k_index)

    # 人工确认:给「所有」匹配的事件组打 acked —— 不只是冲突,单源事件也要能被人工放行
    acks = load_acknowledged()
    if acks:
        for _tk, _gs in all_groups.items():
            for _g in _gs:
                _a = _ack_match(acks, _g.ticker, _g.etype, _g.anchor_date)
                if _a:
                    _g.acked = True
                    # 合约 3% 判定只能接受 etype+日期精确匹配且带值的确认；
                    # 旧的 ticker-wide / 无类型宽键不再生效，避免同日异类型互相放行。
                    _g.ack_exact = bool(
                        _a.get("date") == _g.anchor_date and _a.get("etype") == _g.etype
                    )
                    _g.ack_value = _a.get("value") if _a.get("value") not in (None, "") else None

    # 在事件进入 new / announced / pending / calendar 之前统一生成合约动作结论。
    # 现金分红使用本轮缓存的最近已完成交易日收盘价；缺价或数值未过门禁则 review。
    for _tk, _groups in all_groups.items():
        for _g in _groups:
            _decl = R.pick_value(_g.by_source, "declaration_date")
            # “事件是否成立”和“数值能否执行”必须分开。已有公司宣告、官方来源
            # 或第二源时，事件本身照常正式展示；金额/比例冲突由 contract policy
            # 单独降为待核实，不能把整条公司行动误写成预测。
            _confirmed = (R.has_official_source(_g.by_source) or bool(_decl) or
                          len(_g.by_source) >= 2)
            _forecast = bool(_g.is_future and _g.etype != "filing" and not _confirmed)
            attach_product_action(_g, reference_prices.get(_tk), business_today(), _forecast)

    watches = active_forecast_watches(load_forecast_watches())
    state = load_state()
    seen, fired, declared = state["seen"], state["fired_rounds"], state["declared"]
    forecast_state = state["forecast_status"]
    contract_state = state["contract_action_status"]
    today_d = business_today()
    today = today_d.isoformat()
    cutoff30 = (today_d - dt.timedelta(days=30)).isoformat()
    new_events, round_alerts, conflicts, gaps, pending, announced = [], [], [], [], [], []
    forecasts, forecast_updates, contract_updates = [], [], []
    forecast_sigs, matched_watch_keys = set(), set()

    # 新标的首次纳入监控时是否静默建基线(见 config.BASELINE_NEW_TICKERS):
    #   开 → 把它的历史事件记为「已见」但不推「新发现」,避免上新一批标的时刷屏
    #   关(默认)→ 照常推,能一次看全新标的的存量事件
    known_tickers = {s.split("|", 1)[0] for s in seen}

    for tk, groups in all_groups.items():
        first_time_ticker = getattr(C, "BASELINE_NEW_TICKERS", False) and tk not in known_tickers
        for g in groups:
            s = sig(g)
            if s not in seen:
                seen[s] = today
                if not first_time_ticker:
                    new_events.append(g)
            if g.conflicts:
                conflicts.append(g)
            if g.gaps:
                gaps.append(g)

            def _pk(f, _g=g):
                return R.pick_value(_g.by_source, f)

            def _nsrc(f, _g=g):
                return R.n_src(_g.by_source, f)

            _amt_srcs = max(_nsrc("amount"), _nsrc("ratio"))
            _official = R.has_official_source(g.by_source)
            watch = match_forecast_watch(watches, g.ticker, g.etype, g.anchor_date or "")
            prior_watch = match_forecast_watch(
                watches, g.ticker, g.etype, g.anchor_date or "",
                statuses=("watching", "confirmed", "expired"),
            )
            # 只有仍在未来的事件才算持续命中观察项。过期却仍留在历史缓存的
            # 单源记录不能阻止「预测失效」提醒。
            if watch and g.is_future:
                matched_watch_keys.add(forecast_key(watch))

            # 📣 新公告:首次出现 declaration date 即推送(即使之前见过其预估)
            decl = _pk("declaration_date")
            if decl and declared.get(s) != decl:
                declared[s] = decl
                # 只推近窗口(避免首跑回填历史):宣告日近 30 天内,或事件未来/刚过
                near = g.is_future or ((g.anchor_date or "") >= cutoff30)
                if decl >= cutoff30 and near:
                    announced.append({"ticker": g.ticker, "etype": g.etype, "date": g.anchor_date,
                                      "decl": decl, "days": g.days_to,
                                      "record": _pk("record_date"), "pay": _pk("pay_date"),
                                      **_event_value_fields(g), "amt_srcs": _amt_srcs,
                                      "acked": getattr(g, "acked", False),
                                      "official": _official,
                                      "forecast_watch": bool(prior_watch),
                                      "products": C.product_tags(g.ticker),
                                      **_product_fields(g)})

            if g.is_future and g.etype != "filing" and g.days_to is not None:
                # 持续展示所有正式未来事件；只有产品动作门槛命中的项目才进入周期提醒。
                _decl = _pk("declaration_date")
                # 正式跟踪判定：已逐项核验的 CompanyIR、已取得宣告日，或 ≥2 源一致。
                # 普通单源且无宣告日仍是预测，只能进入核验提醒，不能让运营执行。
                _confirmed = (_official or bool(_decl) or len(g.by_source) >= 2)
                event = {"ticker": g.ticker, "etype": g.etype, "date": g.anchor_date,
                         "days": g.days_to, "status": g.status,
                         "decl": _decl, "record": _pk("record_date"),
                         "pay": _pk("pay_date"), **_event_value_fields(g),
                         "amt_srcs": _amt_srcs, "acked": getattr(g, "acked", False),
                         "official": _official,
                         "first": _decl or seen.get(s), "confirmed": _confirmed,
                         "forecast": not _confirmed,
                         "srcs": sorted(g.by_source.keys()), "products": C.product_tags(g.ticker),
                         **_product_fields(g), "watching": bool(watch),
                         "watch_note": (watch or {}).get("note", "")}
                if not _confirmed:
                    forecast_sigs.add(s)
                    forecasts.append(event)
                    is_single_forecast = len(g.by_source) == 1 and not _decl and not R.is_disputed(g)
                    # 自动识别的单源也要持久化状态，不能只有人工 `观察` 的事件
                    # 才能在补到宣告/第二源时发出「已转正式」。
                    status_keys = {s} if is_single_forecast else set()
                    if watch:
                        status_keys.add(forecast_key(watch))
                    status_update_emitted = False
                    for wk in status_keys:
                        prev = forecast_state.get(wk) or {}
                        snapshot = {"status": "watching", "date": g.anchor_date,
                                    "amount": event.get("amount"), "last_seen": today,
                                    "ticker": g.ticker, "etype": g.etype,
                                    "automatic": wk == s}
                        if not status_update_emitted and prev and prev.get("status") == "watching" and (
                                prev.get("date") != snapshot["date"] or prev.get("amount") != snapshot["amount"]):
                            forecast_updates.append({**event, "kind": "updated",
                                                     "previous_date": prev.get("date"),
                                                     "previous_amount": prev.get("amount")})
                            status_update_emitted = True
                        forecast_state[wk] = snapshot
                    # 单源预测也必须被看见：沿用 30 天首次知会、14 天内每日提醒，
                    # 但提醒内容只要求核验，明确禁止据此执行公司行动。
                    if is_single_forecast:
                        reminder = schedule_event_reminder(event, s, fired, today)
                        if reminder:
                            round_alerts.append(reminder)
                    continue

                pending.append(event)
                if g.ticker in C.CONTRACT_TICKERS:
                    action = event.get("contract_action") or {}
                    current_action = {
                        "status": action.get("status"),
                        "impact_pct": action.get("impact_pct"),
                        "price_as_of": action.get("price_as_of"),
                        "last_seen": today,
                    }
                    previous_action = contract_state.get(s)
                    if (isinstance(previous_action, dict) and previous_action.get("status") and
                            previous_action.get("status") != current_action["status"]):
                        contract_updates.append({
                            **event,
                            "previous_status": previous_action.get("status"),
                            "current_status": current_action["status"],
                        })
                    elif (not previous_action and s in fired and g.ticker not in C.SPOT_TICKERS and
                          current_action["status"] == "not_required"):
                        # 老版本会把所有正式合约事件都当执行催办。升级时若该事件已经
                        # 留下旧 fired 记录而新结论为无需操作，主动发一次解除，不能静默消失。
                        contract_updates.append({
                            **event,
                            "previous_status": "legacy_execution",
                            "current_status": "not_required",
                            "migration_resolution": True,
                        })
                    contract_state[s] = current_action
                transition_keys = {s}
                if prior_watch:
                    transition_keys.add(forecast_key(prior_watch))
                previous_forecasts = [
                    forecast_state.get(wk) or {} for wk in transition_keys
                    if (forecast_state.get(wk) or {}).get("status") == "watching"
                ]
                if previous_forecasts:
                    prev = previous_forecasts[0]
                    event["promoted_from_forecast"] = True
                    forecast_updates.append({**event,
                                             "kind": "declared" if _decl else "promoted",
                                             "previous_date": prev.get("date") or (prior_watch or {}).get("date"),
                                             "previous_amount": prev.get("amount"),
                                             "confirmation_source": getattr(g, "primary_url", "")})
                for wk in transition_keys:
                    forecast_state[wk] = {"status": "confirmed", "date": g.anchor_date,
                                          "amount": event.get("amount"), "last_seen": today,
                                          "ticker": g.ticker, "etype": g.etype,
                                          "automatic": wk == s}
                unresolved_conflict = bool(g.conflicts) and not getattr(g, "acked", False)
                if (event.get("follow_up_mode") != "none" and
                        not (event.get("follow_up_mode") == "verification" and unresolved_conflict)):
                    reminder = schedule_event_reminder(event, s, fired, today)
                    if reminder:
                        round_alerts.append(reminder)

    # 观察项若直到预计日仍未得到任何匹配事件，明确通知「预测失效」而不是悄悄消失。
    for watch in watches:
        wk = forecast_key(watch)
        if wk in matched_watch_keys or watch.get("status", "watching") != "watching":
            continue
        prev = forecast_state.get(wk) or {}
        if (watch.get("date", "") < today and prev.get("status") == "watching"):
            forecast_updates.append({"ticker": watch.get("ticker"), "etype": watch.get("etype"),
                                     "date": watch.get("date"), "kind": "expired",
                                     "watching": True, "watch_note": watch.get("note", ""),
                                     "srcs": [], "products": C.product_tags(watch.get("ticker", ""))})
            forecast_state[wk] = {"status": "expired", "date": watch.get("date"), "last_seen": today}

    # 统一「首发日」:分红宣告日(declaration date)→ 否则监控首次发现日
    for tk, groups in all_groups.items():
        for g in groups:
            g.forecast = sig(g) in forecast_sigs
            decl = R.pick_value(g.by_source, "declaration_date")
            g.first_announced = decl or seen.get(sig(g))

    cutoff = (today_d - dt.timedelta(days=30)).isoformat()
    new_events = [g for g in new_events
                  if (g.anchor_date or "") >= cutoff and sig(g) not in forecast_sigs]
    new_events.sort(key=lambda g: g.anchor_date or "", reverse=True)
    round_alerts.sort(key=lambda x: x["days"])
    conflicts.sort(key=lambda g: g.anchor_date or "", reverse=True)
    gaps.sort(key=lambda g: g.anchor_date or "", reverse=True)
    pending.sort(key=lambda x: x["days"])
    announced.sort(key=lambda x: x.get("decl") or "", reverse=True)
    forecasts.sort(key=lambda x: x["days"])

    # 人工确认:把已确认的冲突/空缺从报警里剔除(停推+网页 finalize),记入 resolved
    # 确认 = 人工已核实该事件 → 冲突和空缺**一起**消(和机器人 apply_acks 口径一致);
    # 否则会出现"值确认了、空缺还挂着"的怪象(NOK/SONY 就是)。
    resolved = []
    if acks:
        _res_seen = set()
        _active = []
        for g in conflicts:
            a = _ack_match(acks, g.ticker, g.etype, g.anchor_date)
            if a:
                resolved.append({"ticker": g.ticker, "etype": g.etype, "date": g.anchor_date,
                                 "value": a.get("value"), "by": a.get("by"), "at": a.get("at"),
                                 "detail": "; ".join(g.conflicts)})
                _res_seen.add((g.ticker, g.etype, g.anchor_date))
            else:
                _active.append(g)
        conflicts = _active
        _active_gaps = []
        for g in gaps:
            a = _ack_match(acks, g.ticker, g.etype, g.anchor_date)
            if a:
                if (g.ticker, g.etype, g.anchor_date) not in _res_seen:
                    resolved.append({"ticker": g.ticker, "etype": g.etype, "date": g.anchor_date,
                                     "value": a.get("value"), "by": a.get("by"), "at": a.get("at"),
                                     "detail": "; ".join(g.gaps)})
                    _res_seen.add((g.ticker, g.etype, g.anchor_date))
            else:
                _active_gaps.append(g)
        gaps = _active_gaps

    # ---- 展示金额门禁：必须在网页、digest 和 Lark 渲染之前完成 ----
    # 未解决的冲突只展示各源候选值，绝不能先挑一个金额写进“已确认未来/临近提醒”。
    # 人工确认值则在所有 dict 展示副本中统一覆盖；合约 3% 判定仍使用上面的
    # 更严格币种/证券单位门禁，两者不能混为一谈。
    _disputed = {}
    for g in conflicts:
        vals = {}
        for source_name, fields in g.by_source.items():
            value = fields.get("amount") if fields.get("amount") is not None else fields.get("ratio")
            if value is not None:
                vals[source_name] = value
        _disputed[sig(g)] = {"detail": "; ".join(g.conflicts), "vals": vals}

    def _apply_display_value_contract(events):
        for e in events:
            for ack in acks:
                if ack.get("value") in (None, ""):
                    continue
                if (e.get("ticker") != ack.get("ticker") or
                        e.get("etype") != ack.get("etype") or
                        e.get("date") != ack.get("date")):
                    continue
                if e.get("etype") == "dividend":
                    try:
                        value = float(ack["value"])
                    except (TypeError, ValueError):
                        continue
                    e["amount"] = value
                    e["value_display"] = CP.value_display(
                        "dividend",
                        amount=value,
                        subtype=e.get("subtype", ""),
                        amount_currency=e.get("amount_currency", ""),
                        amount_unit=e.get("amount_unit", ""),
                    )
                elif e.get("etype") == "split":
                    ratio = S.normalize_ratio(ack.get("value"))
                    if ratio:
                        e["ratio"] = ratio
                        e["value_display"] = CP.value_display("split", ratio=ratio)
            disputed = _disputed.get(
                f"{e.get('ticker')}|{e.get('etype')}|{e.get('date')}"
            )
            if disputed:
                e["disputed"] = True
                e["dispute_vals"] = disputed["vals"]
                e["dispute_detail"] = disputed["detail"]
                e["amount"] = None
                e["ratio"] = None
                e["value_display"] = ""

    _apply_display_value_contract(
        pending + forecasts + forecast_updates + contract_updates + round_alerts + announced
    )

    # 所有展示面共用同一份引用契约：先把 event group 和每类 dict 都补全，
    # 避免「单标的卡/推送/网页」各自回退到不同链接。
    for groups in all_groups.values():
        for g in groups:
            attach_event_references(g, refs_config, sec8k_index)
    for e in pending + forecasts + forecast_updates + contract_updates + round_alerts + announced:
        attach_event_references(e, refs_config, sec8k_index)

    # ---- 人工介入闭环:每条异常挂多久没人确认(不豁免、不自动消失、每次跑都重报)----
    # 异常 = 字段冲突 / 数据空缺。未宣告的单源预估走「预测观察」自动追踪，
    # 不进入人工确认积压；临近日只生成「核验提醒」，不生成执行催办。
    # 唯一消解方式:群里发「确认 代码 [正确值]」。挂越久越显眼,超 REVIEW_ESCALATE_DAYS 天在推送里 @ 人升级。
    review = state.setdefault("review", {})
    def _age(key):
        first = review.get(key)
        if not first:
            review[key] = today
            return 0
        try:
            return (today_d - dt.date.fromisoformat(first)).days
        except Exception:
            return 0

    open_keys = set()
    for g in conflicts:
        k = f"{sig(g)}#conflict"; open_keys.add(k); g.age_days = _age(k)
    for g in gaps:
        k = f"{sig(g)}#gap"; open_keys.add(k); g.age_days = _age(k)
    # 已被人工确认 / 事件已消失的,从待办里清掉(只有这两种情况才会消失)
    for k in list(review):
        if k not in open_keys:
            review.pop(k, None)

    ages = [g.age_days for g in conflicts] + [g.age_days for g in gaps]
    esc = C.REVIEW_ESCALATE_DAYS
    review_summary = {"open": len(open_keys), "overdue": sum(1 for a in ages if a >= esc),
                      "max_age": max(ages) if ages else 0, "escalate_days": esc,
                      "conflicts": len(conflicts), "gaps": len(gaps),
                      "unconfirmed": 0}

    alerts = {"new": new_events, "rounds": round_alerts, "conflicts": conflicts,
              "gaps": gaps, "pending": pending, "announced": announced, "resolved": resolved,
              "forecasts": forecasts, "forecast_updates": forecast_updates,
              "contract_updates": contract_updates, "review": review_summary}
    meta = {"generated": _now_label(), "business_date": today}

    # 单页站点:日历 + 预警面板(标签切换)
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(RP.build_site(all_groups, source_health, alerts, meta))
    digest = RP.build_text_digest(alerts, meta)
    with open(OUT_DIGEST, "w", encoding="utf-8") as f:
        f.write(digest)

    # 月历事件(供交互机器人画当月月历):近 45 天~未来 80 天内的分红/拆股/并购退市
    cal_lo = (today_d - dt.timedelta(days=45)).isoformat()
    cal_hi = (today_d + dt.timedelta(days=80)).isoformat()
    forecast_event_keys = {(x.get("ticker"), x.get("etype"), x.get("date")) for x in forecasts}
    calendar_events = []
    for tk, groups in all_groups.items():
        for g in groups:
            ad = g.anchor_date or ""
            if not (cal_lo <= ad <= cal_hi):
                continue
            if g.etype == "filing":
                if not any(k in (g.note or "") for k in ("并购", "退市", "分拆", "证券变更", "要约")):
                    continue
            elif g.etype not in ("dividend", "split"):
                continue
            def _ck(f, _g=g):
                return R.pick_value(_g.by_source, f)
            entry = {"ticker": g.ticker, "etype": g.etype, "date": ad,
                     **_event_value_fields(g), "note": g.note,
                     "amt_srcs": max(R.n_src(g.by_source, "amount"), R.n_src(g.by_source, "ratio")),
                     "acked": getattr(g, "acked", False),
                     "official": R.has_official_source(g.by_source),
                     "record": _ck("record_date"), "pay": _ck("pay_date"),
                     "decl": _ck("declaration_date"),
                     "first": getattr(g, "first_announced", None),
                     "status": g.status, **_product_fields(g),
                     "forecast": (g.ticker, g.etype, ad) in forecast_event_keys,
                     "url": (g.by_source.get("SEC") or {}).get("url", "") if g.etype == "filing" else "",
                     "srcs": sorted(g.by_source.keys()),
                     "products": C.product_tags(g.ticker)}
            attach_event_references(entry, refs_config, sec8k_index)
            # 月历主块点击官方来源；filing 仍指向 SEC 原文。
            if entry.get("primary_url"):
                entry["url"] = entry["primary_url"]
            calendar_events.append(entry)
    _apply_display_value_contract(calendar_events)

    # 资产覆盖(现货/合约 × 标的类型 × 是否监控)
    TYPE_CN = {"equity": "个股", "etf": "ETF", "commodity": "商品/外汇", "foreign": "海外股"}
    coverage = []
    for tk in C.ALL_ASSETS:
        coverage.append({"ticker": tk, "name": C.NAMES.get(tk, ""),
                         "spot": tk in C.SPOT_TICKERS, "contract": tk in C.CONTRACT_TICKERS,
                         "type": C.asset_type(tk), "type_cn": TYPE_CN.get(C.asset_type(tk), C.asset_type(tk)),
                         "monitored": C.is_monitored(tk)})

    # 最近宣告(declaration)的事件:取最新 5 个,已派发完的标 ended
    today_iso = today
    recent_declares = []
    for tk, groups in all_groups.items():
        for g in groups:
            decl = R.pick_value(g.by_source, "declaration_date")
            if not decl:
                continue
            def _dk(f, _g=g):
                return R.pick_value(_g.by_source, f)
            pay = _dk("pay_date")
            end_date = pay or g.anchor_date or ""
            ended = bool(end_date) and end_date < today_iso
            try:
                days = (dt.date.fromisoformat(g.anchor_date) - today_d).days if g.anchor_date else None
            except Exception:
                days = None
            entry = {"ticker": g.ticker, "etype": g.etype, "date": g.anchor_date,
                     "decl": decl, "record": _dk("record_date"), "pay": pay,
                     **_event_value_fields(g),
                     "amt_srcs": max(R.n_src(g.by_source, "amount"), R.n_src(g.by_source, "ratio")),
                     "official": R.has_official_source(g.by_source),
                     "srcs": sorted(g.by_source.keys()), "days": days, "ended": ended,
                     "products": C.product_tags(g.ticker), **_product_fields(g)}
            attach_event_references(entry, refs_config, sec8k_index)
            recent_declares.append(entry)
    _apply_display_value_contract(recent_declares)
    recent_declares.sort(key=lambda x: x.get("decl") or "", reverse=True)
    recent_declares = recent_declares[:5]

    # conflicts/new 已从 all_groups 预挂引用；此处只发布当前覆盖范围内的扁平 IR 映射给
    # 独立部署的 Bot，避免已退出现货/合约范围的历史参考链接继续出现在活动面板或 Bot。
    # 写审计日志时仍不依赖 Railway 容器能否读取仓库根的 refs.json。
    ir_map = {
        ticker: url
        for ticker, url in (refs_config.get("ir_dividend", {}) or {}).items()
        if ticker in C.ALL_ASSETS
    }
    ticker_aliases = {
        alias: ticker
        for alias, ticker in getattr(C, "TICKER_ALIASES", {}).items()
        if ticker in C.ALL_ASSETS
    }

    # 发布给交互机器人读取的数据(随 Pages 一起部署为 data.json)
    site_data = {
        "generated": meta["generated"],
        "business_date": today,
        "changelog": load_changelog(),
        "coverage": coverage,
        "counts": {"pending": len(pending), "forecasts": len(forecasts), "new": len(new_events),
                   "conflicts": len(conflicts), "gaps": len(gaps),
                   "announced": len(announced)},
        "announced": announced,
        "recent_declares": recent_declares,
        "resolved": resolved,
        "refs": ir_map,
        "ticker_aliases": ticker_aliases,
        "pending": pending,
        "forecasts": forecasts,
        "forecast_updates": forecast_updates,
        "contract_updates": contract_updates,
        "new": [_grp_brief(g) for g in new_events],
        "conflicts": [_grp_brief(g) for g in conflicts],
        "gaps": [_grp_brief(g) for g in gaps],
        "calendar": calendar_events,
    }
    with open(OUT_SITEDATA, "w", encoding="utf-8") as f:
        json.dump(site_data, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 50 + "\n" + digest + "\n" + "=" * 50)
    print(f"\n站点(日历+面板): {OUT_HTML}\nDigest: {OUT_DIGEST}")

    # 生产投递失败时不写 state，下次继续重试而不是静默吞掉预警。
    deliver_then_save(alerts, meta, state)
    return alerts


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "fetch":
        fetch([t.upper() for t in args[1:]] or C.TICKERS)
    elif args and args[0] == "build":
        build()
    else:
        fetch([t.upper() for t in args] or C.TICKERS)
        build()
