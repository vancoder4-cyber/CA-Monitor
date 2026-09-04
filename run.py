# -*- coding: utf-8 -*-
"""主流程:抓取 → 核对 → 报警 → 输出面板+digest。

两段式(绕过单次运行时限,也便于调度):
    python run.py fetch [T1 T2 ...]   # 抓取+核对指定票(默认全量),结果缓存到 data/cache/
    python run.py build               # 合并所有缓存 → 计算报警 → 写 dashboard.html + digest
    python run.py                     # = fetch 全量 + build(一次跑完,适合定时任务)

状态文件 data/state.json:已见事件签名(新发现判定)+ 已触发预警轮次(去重)。
"""
import os, sys, json, re, hashlib, datetime as dt, math
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


def _provenance_meta(now_utc=None):
    """生成供 Pages、问答助手和发布验收共同使用的机器可读版本信息。"""
    now_utc = (now_utc or dt.datetime.now(dt.timezone.utc)).astimezone(
        dt.timezone.utc
    ).replace(microsecond=0)
    now_et = business_now(now_utc)
    # GitHub 定时任务可能排队；允许下一计划扫描点后 4 小时宽限。超过这个
    # 明确时点，网站与 Bot 必须显示数据不可用，不能只因 business_date 相同
    # 就在一天内持续“假绿”。
    slots = ((9, 35), (12, 45), (16, 5))
    next_run = None
    # main push / Bot 状态写回也会触发构建。周末不能把“当天尚未到的工作日
    # 扫描时点”当作下一次刷新，否则周六上午发布的数据会在周六下午过期，
    # 网站与 Bot 一直 fail closed 到周一。这里只在工作日寻找当天剩余 slot。
    if now_et.weekday() < 5:
        for hour, minute in slots:
            candidate = now_et.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate > now_et:
                next_run = candidate
                break
    if next_run is None:
        day = now_et.date() + dt.timedelta(days=1)
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        next_run = now_et.replace(
            year=day.year, month=day.month, day=day.day,
            hour=slots[0][0], minute=slots[0][1], second=0, microsecond=0,
        )
    valid_until = (next_run + dt.timedelta(hours=4)).astimezone(dt.timezone.utc)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    run_url = f"https://github.com/{repo}/actions/runs/{run_id}" if repo and run_id else ""
    return {
        "schema_version": C.PUBLIC_DATA_SCHEMA_VERSION,
        "generated_at_utc": now_utc.isoformat().replace("+00:00", "Z"),
        "valid_until_utc": valid_until.isoformat().replace("+00:00", "Z"),
        "source_sha": os.environ.get("GITHUB_SHA", "") or "local",
        "run_id": run_id,
        "run_url": run_url,
    }
CACHE = os.path.join(DATA, "cache")
os.makedirs(CACHE, exist_ok=True)
STATE_PATH = os.path.join(DATA, "state.json")
REFERENCE_PRICE_PATH = os.path.join(DATA, "reference_prices.json")
FORECAST_WATCH_PATH = os.path.join(DATA, "forecast_watch.json")
FILING_RESOLUTION_PATH = os.path.join(DATA, "filing_review_resolutions.json")
OUT_HTML = os.path.join(HERE, "dashboard.html")
OUT_DIGEST = os.path.join(DATA, "latest_digest.txt")
OUT_SITEDATA = os.path.join(HERE, "site_data.json")  # 供交互机器人读取(会发布到 Pages/data.json)


def load_changelog():
    """复用网页解析器，保证 Pages/Bot 与网站的更新日志层级完全一致。"""
    return RP.load_changelog(os.path.join(HERE, "CHANGELOG.md"))


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


def load_filing_review_resolutions():
    """读取 Bot 写回的 SEC filing 核验结论。

    每条必须使用带文件指纹的完整 event_id；无效/旧宽键 fail closed，
    不得影响同标的同日其它 SEC 文件。
    """
    if not os.path.exists(FILING_RESOLUTION_PATH):
        return []
    try:
        with open(FILING_RESOLUTION_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    valid = []
    for item in data:
        if not isinstance(item, dict) or item.get("status") not in {"confirmed", "routine"}:
            continue
        event_id = item.get("event_id") or ""
        if not re.fullmatch(
                r"[A-Z0-9.-]+\|filing\|\d{4}-\d{2}-\d{2}\|[0-9a-f]{12}",
                event_id):
            continue
        valid.append(item)
    return valid


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
    verification_kind = event.get("verification_kind")
    if verification_kind not in {"forecast", "filing_terms", "contract_threshold"}:
        # 兼容尚未带中央字段的旧状态/测试夹具；新 payload 一律由
        # attach_product_action 明确下发 verification_kind。
        if event.get("forecast"):
            verification_kind = "forecast"
        elif event.get("follow_up_mode") == "verification":
            verification_kind = (
                "filing_terms"
                if event.get("etype") == "filing" and event.get("filing_relevant") is None
                else "contract_threshold"
            )
        else:
            verification_kind = ""
    if verification_kind == "forecast":
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
    if verification_kind == "forecast":
        reminder.update({
            "ops": "🔎 单源待核实：请核对公司官方公告或第二个独立源；未确认前勿执行。",
            "risk_copy": "数据核验提醒，不是公司行动执行指令。",
            "risk": [],
            "verification": True,
            "verification_kind": "forecast",
        })
    elif verification_kind == "filing_terms":
        reminder.update({
            "ops": "🔎 公司行动条款核验：请打开 SEC 原文确认事件类型、生效日与处理条款；核实前勿执行。",
            "risk_copy": "公司行动条款核验提醒，核实前不得执行。",
            "verification": True,
            "verification_kind": "filing_terms",
        })
    elif verification_kind == "contract_threshold":
        reminder.update({
            "ops": "🔎 合约门槛待核实：请补齐可靠金额/比例或参考价；确认影响严格超过 3% 前不执行合约调整。",
            "risk_copy": "合约门槛核验提醒，不是执行指令。",
            "verification": True,
            "verification_kind": "contract_threshold",
        })
    else:
        reminder.update({
            "ops": C.alert_copy(days, event.get("etype")),
            "risk_copy": C.ROUND_RISK_TBD,
            "verification": False,
            "verification_kind": "",
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
    """只接受精确事件且值有效的确认；旧宽键/空值不得放行异常。"""
    for a in acks:
        value = a.get("value") if isinstance(a, dict) else None
        value_valid = False
        if etype == "split":
            normalized = S.normalize_ratio(value)
            try:
                new, old = (int(part) for part in normalized.split(":"))
                value_valid = new > 0 and old > 0
            except (AttributeError, TypeError, ValueError):
                value_valid = False
        elif (etype == "dividend" and not isinstance(value, bool)
              and re.fullmatch(r"\+?\d+(?:\.\d+)?", str(value).strip())):
            try:
                numeric = float(value)
                value_valid = math.isfinite(numeric) and numeric > 0
            except (TypeError, ValueError):
                value_valid = False
        if (value_valid and a.get("ticker") == ticker and a.get("etype") == etype
                and a.get("date") == date):
            return a
    return None


def _ack_display_value(ack, etype):
    """旧 split factor 只在读取时兼容；对外始终显示完整 new:old。"""
    value = (ack or {}).get("value")
    return S.normalize_ratio(value) if etype == "split" else value


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


def _dividend_references(ticker, date, decl, refs, sec8k, *, linked_sec_url="",
                         linked_sec_form="6-K"):
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
    if linked_sec_url:
        add(f"SEC·本次宣告 {linked_sec_form or '6-K'}", linked_sec_url,
            "official_filing")
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
    linked_sec_url = (target.get("linked_sec_url", "") if isinstance(target, dict)
                      else getattr(target, "linked_sec_url", ""))
    linked_sec_form = (target.get("linked_sec_form", "6-K") if isinstance(target, dict)
                       else getattr(target, "linked_sec_form", "6-K"))
    links = _dividend_references(
        ticker, date, decl, refs, sec8k,
        linked_sec_url=linked_sec_url,
        linked_sec_form=linked_sec_form,
    )
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
    fields = {
        "event_id": getattr(g, "event_id", ""),
        "contract_action": getattr(g, "contract_action", {}),
        "follow_up_mode": getattr(g, "follow_up_mode", "execution"),
        "verification_kind": getattr(g, "verification_kind", ""),
        "reminder_state_suffix": getattr(g, "reminder_state_suffix", ""),
        "filing_relevant": getattr(g, "filing_relevant", None),
        "filing_resolution_status": getattr(g, "filing_resolution_status", ""),
        "linked_event_id": getattr(g, "linked_event_id", ""),
        "risk": getattr(g, "risk", []),
    }
    if getattr(g, "linked_sec_url", ""):
        fields.update({
            "linked_sec_url": g.linked_sec_url,
            "linked_sec_form": getattr(g, "linked_sec_form", "6-K"),
            "linked_filing_event_id": getattr(g, "linked_filing_event_id", ""),
        })
    if getattr(g, "etype", "") == "filing":
        sec_url = ((getattr(g, "by_source", {}) or {}).get("SEC") or {}).get("url", "")
        fields.update({"url": sec_url, "sec_url": sec_url, "src_url": sec_url})
    return fields


def _is_routine_filing(event):
    """已明确为普通备案的 filing 只进 SEC 原文表，不进 CA 流。"""
    return (getattr(event, "etype", None) == "filing" and
            getattr(event, "filing_relevant", None) is False)


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
    # Preserve source-provided tri-state relevance.  In particular, SEC 6-K
    # metadata hints use explicit None to mean "suspected, verify terms".  The
    # translated note contains words such as 并购/分拆, but those words must not
    # upgrade the filing to an executable action before its terms are read.
    provided_relevance = [
        x.get("relevant") for x in (g.by_source or {}).values()
        if "relevant" in x
    ]
    keyword_relevant = any(token in note for token in (
            "并购", "退市", "分拆", "证券变更", "要约", "merger", "spin_off",
            "spin-off", "name_change", "symbol_change", "redemption", "worthless",
        ))
    resolution_status = getattr(g, "filing_resolution_status", "")
    if g.etype == "filing" and resolution_status == "confirmed":
        filing_relevant = True
    elif g.etype == "filing" and resolution_status in {"routine", "linked"}:
        filing_relevant = False
    elif any(value is True for value in provided_relevance):
        filing_relevant = True
    elif any(value is None for value in provided_relevance):
        filing_relevant = None
    elif provided_relevance and all(value is False for value in provided_relevance):
        filing_relevant = False
    elif keyword_relevant:
        # Legacy groups without a structured relevance field retain the old
        # conservative heuristic.  All current SEC/Alpaca/FINX events carry
        # the field explicitly and therefore never rely on translated text.
        filing_relevant = True
    else:
        # Alpaca / FINX “其它公司行动”没有足够条款时必须 review，不能当普通备案 no-op。
        filing_relevant = None
    g.filing_relevant = filing_relevant
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
    # 预测和条款未知的结构性 filing 无论是否含现货都只能核验，不能因为
    # 现货默认流程被误标为 execution。尤其 6-K 元数据只命中提示词时，尚未
    # 解析出真实条款，绝不能据此要求下架、调合约或触发正式执行 @。
    needs_filing_verification = g.etype == "filing" and filing_relevant is None
    mode = ("verification" if forecast or needs_filing_verification
            else CP.follow_up_mode(g.ticker, decision))
    verification_kind = (
        "forecast" if forecast else
        "filing_terms" if needs_filing_verification else
        "contract_threshold" if mode == "verification" else
        ""
    )
    g.contract_action = decision
    g.follow_up_mode = mode
    g.verification_kind = verification_kind
    # Suspected filing terms need their own cadence key even for spot-only
    # symbols.  If the filing is later confirmed, the resulting execution
    # reminder must not be swallowed by the earlier verification reminder.
    g.reminder_state_suffix = (
        "filing-review" if verification_kind == "filing_terms"
        else CP.reminder_state_suffix(g.ticker, decision, mode)
    )
    g.risk = C.risk_note(
        g.ticker,
        g.etype,
        decision,
        forecast=forecast,
        filing_relevant=filing_relevant,
    )
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


def _filing_relevance_status(value, resolution_status=""):
    """把 filing 三态压成可持久化的稳定状态名。"""
    if resolution_status in {"confirmed", "routine", "linked"}:
        return resolution_status
    if value is True:
        return "confirmed"
    if value is False:
        return "routine"
    return "review"


def track_filing_relevance(g, event_id, filing_state, today):
    """记录 filing 条款核验状态，并返回一次性的状态迁移事件。

    filing 的锚点通常就是 SEC 申报日，第二天便不再属于 future。状态迁移因此
    不能放在未来事件分支里，否则 ``待核实 -> 已确认/普通备案`` 会静默消失。
    这里按稳定 event_id 独立追踪；调用方只在投递成功后保存 state，所以失败
    会在下轮原样重试。
    """
    if getattr(g, "etype", "") != "filing":
        return None

    current = _filing_relevance_status(
        getattr(g, "filing_relevant", None),
        getattr(g, "filing_resolution_status", ""),
    )
    previous_entry = filing_state.get(event_id)
    previous = (
        previous_entry.get("status") if isinstance(previous_entry, dict)
        else previous_entry if previous_entry in {"review", "confirmed", "routine"}
        else None
    )
    event = {
        **_grp_brief(g),
        "days": g.days_to,
        "status": g.status,
        "decl": R.pick_value(g.by_source, "declaration_date"),
        "record": R.pick_value(g.by_source, "record_date"),
        "pay": R.pick_value(g.by_source, "pay_date"),
        "products": C.product_tags(g.ticker),
    }
    # 已过期的元数据提示若仍只是 review，不能在每次抓取时重开。
    # 只有来源变为 confirmed/routine，或操作员写入明确结论才重开。
    persisted_status = "expired" if previous == "expired" and current == "review" else current
    snapshot = {
        "status": persisted_status,
        "last_seen": today,
        # 即使某一轮来源暂时漏数，也能继续提醒尚未关闭的条款核验。
        "event": event,
    }
    if isinstance(previous_entry, dict) and previous_entry.get("last_review_alert"):
        snapshot["last_review_alert"] = previous_entry["last_review_alert"]
    filing_state[event_id] = snapshot

    if persisted_status == "expired":
        return None
    if previous not in {"review", "expired"} or current == "review":
        return None

    update = {
        **event,
        "kind": current,
        "previous_status": "review",
        "current_status": current,
        "transition_date": today,
        "filing_relevance_update": True,
    }
    if current in {"routine", "linked"}:
        # 现货产品通常默认 execution；普通备案的解除通知必须显式覆盖为 no-op，
        # 不能因为它曾经是疑似公司行动而误 @ 执行负责人。
        update.update({
            "follow_up_mode": "none",
            "verification_kind": "",
            "reminder_state_suffix": "",
        })
    return update


def collect_overdue_filing_reviews(filing_state, today):
    """提醒事件日已过的 filing；满期后停止日报，但不作无需操作判断。"""
    updates = []
    for event_id, entry in filing_state.items():
        if not isinstance(entry, dict) or entry.get("status") != "review":
            continue
        event = entry.get("event")
        if not isinstance(event, dict) or entry.get("last_review_alert") == today:
            continue
        try:
            days = (dt.date.fromisoformat(str(event.get("date"))) -
                    dt.date.fromisoformat(today)).days
        except (TypeError, ValueError):
            continue
        if days >= 0:
            # D0 仍由既有 30/14 cadence 提醒；这里只补事件日之后的盲区。
            continue
        if days < -getattr(C, "FILING_REVIEW_EXPIRE_DAYS", 30):
            updates.append({
                **event,
                "event_id": event_id,
                "days": days,
                "kind": "expired",
                "previous_status": "review",
                "current_status": "expired",
                "transition_date": today,
                "filing_relevance_update": True,
                "follow_up_mode": "none",
                "verification_kind": "filing_terms",
                "reminder_state_suffix": "",
                "risk": [],
            })
            entry.update({
                "status": "expired",
                "resolved_at": today,
                "resolution_source": "auto_expiry",
                "last_review_alert": today,
            })
            continue
        updates.append({
            **event,
            "event_id": event_id,
            "days": days,
            "kind": "review_pending",
            "previous_status": "review",
            "current_status": "review",
            "transition_date": today,
            "filing_relevance_update": True,
        })
        entry["last_review_alert"] = today
    return updates


def apply_missing_filing_resolutions(filing_state, resolutions, current_event_ids, today):
    """即使 filing 已从本轮缓存消失，人工结论仍能按 event_id 关闭积压。"""
    updates = []
    by_id = {
        item.get("event_id"): item for item in (resolutions or [])
        if isinstance(item, dict) and item.get("status") in {"confirmed", "routine"}
    }
    for event_id, resolution in by_id.items():
        if event_id in current_event_ids:
            continue
        entry = filing_state.get(event_id)
        if not isinstance(entry, dict) or entry.get("status") not in {"review", "expired"}:
            continue
        event = entry.get("event")
        if not isinstance(event, dict):
            continue
        current = resolution["status"]
        update = {
            **event,
            "event_id": event_id,
            "kind": current,
            "previous_status": entry.get("status"),
            "current_status": current,
            "transition_date": today,
            "filing_relevance_update": True,
            "filing_relevant": current == "confirmed",
            "filing_resolution_status": current,
        }
        if current == "routine":
            update.update({
                "follow_up_mode": "none",
                "verification_kind": "",
                "reminder_state_suffix": "",
                "risk": [],
            })
        else:
            # 当前抓取窗口里已经看不到 filing 时，也必须按“已确认公司行动”
            # 重建产品动作，不能沿用旧 review 快照的 filing_terms/verification。
            ticker = str(update.get("ticker") or "").upper()
            decision = CP.evaluate(
                ticker,
                "filing",
                value_verified=True,
                filing_relevant=True,
                today=today,
            )
            mode = CP.follow_up_mode(ticker, decision)
            verification_kind = "contract_threshold" if mode == "verification" else ""
            update.update({
                "contract_action": decision,
                "follow_up_mode": mode,
                "verification_kind": verification_kind,
                "reminder_state_suffix": CP.reminder_state_suffix(ticker, decision, mode),
                "risk": C.risk_note(
                    ticker,
                    "filing",
                    decision,
                    filing_relevant=True,
                ),
            })
        entry.update({
            "status": current,
            "last_seen": today,
            "resolved_at": today,
            "resolution_source": "operator",
            "event": update,
        })
        updates.append(update)
    return updates


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
            st.setdefault("seen", {})
            st.setdefault("fired_rounds", {})
            st.setdefault("declared", {})   # sig -> 已推送过的宣告日
            st.setdefault("forecast_status", {})  # 自动单源 + 人工观察的状态，供升级/改期/失效通知去重
            st.setdefault("contract_action_status", {})  # sig -> 合约门槛上次结论
            st.setdefault("filing_relevance_status", {})  # 稳定 filing ID -> 条款核验/确认/普通备案状态
            st.setdefault("filing_signature_version", 1)
            return st
    return {"seen": {}, "fired_rounds": {}, "declared": {}, "forecast_status": {},
            "contract_action_status": {}, "filing_relevance_status": {},
            "filing_signature_version": 1}


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


def legacy_sig(g):
    """旧版签名；仅用于无重放迁移。"""
    return f"{g.ticker}|{g.etype}|{g.anchor_date}"


def sig(g):
    """稳定事件 ID。

    分红/拆股一天通常只有一个经济事件，沿用旧签名。filing 同日可能有多份
    不同 SEC 文件（生产曾出现同日 3 份），因此加入来源 URL/表格信息指纹，
    避免后到文件被旧的 ticker+date 去重键吞掉。
    """
    base = legacy_sig(g)
    if getattr(g, "etype", "") != "filing":
        return base
    by_source = getattr(g, "by_source", {}) or {}
    # SEC 原文 URL（含 accession）是 filing 的稳定身份；后续补到 Alpaca/FINX
    # 来源时不能改变 ID 并重放同一事件。没有原文 URL 的供应商事项才退回到
    # 类型/描述指纹，同日不同描述仍保持独立。
    sec_fields = by_source.get("SEC") or {}
    sec_url = sec_fields.get("url") or ""
    identity = ({"sec_url": sec_url} if sec_url else {
        "form": sec_fields.get("form") or "",
        "items": sec_fields.get("items") or "",
        "note": getattr(g, "note", "") or "",
    })
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    return f"{base}|{digest}"


def apply_filing_review_resolutions(all_groups, resolutions, *, allowed_tickers=None):
    """按稳定 filing event_id 应用 Bot/Git 人工结论。

    这一步必须早于 ``attach_product_action`` 和状态 tracker，否则本轮
    仍会产生「待核实」风险/提醒，下轮才关闭。
    """
    allowed = set(C.TICKERS if allowed_tickers is None else allowed_tickers)
    by_id = {
        item.get("event_id"): item for item in (resolutions or [])
        if isinstance(item, dict) and item.get("status") in {"confirmed", "routine"}
        and item.get("ticker", str(item.get("event_id") or "").split("|", 1)[0]) in allowed
    }
    matched = set()
    for groups in all_groups.values():
        for g in groups:
            if g.etype != "filing":
                continue
            event_id = getattr(g, "event_id", "") or sig(g)
            g.event_id = event_id
            resolution = by_id.get(event_id)
            if not resolution:
                continue
            g.filing_resolution_status = resolution["status"]
            g.filing_resolution_source = "operator"
            g.filing_resolution_note = resolution.get("note", "")
            matched.add(event_id)
    return matched


def _suspected_dividend_filing(g):
    """只识别「类型仅为分红/分派」的待核实 SEC 6-K。

    同时出现拆股、并购等任一其它动作词就拒绝自动关联，确保不把
    不同类型的公司行动合并成分红证据。
    """
    if g.etype != "filing":
        return False
    sec = (g.by_source or {}).get("SEC") or {}
    if sec.get("form") not in {"6-K", "6-K/A"} or sec.get("relevant") is not None:
        return False
    note = (g.note or "").lower()
    if not any(word in note for word in ("分红", "分派", "dividend", "distribution")):
        return False
    other_types = (
        "拆股", "合股", "并购", "收购", "分拆", "退市", "要约", "供股",
        "赎回", "名称变更", "代码变更", "split", "merger", "acquisition",
        "spin-off", "spinoff", "delist", "tender", "rights offering", "redemption",
        "name change", "symbol change",
    )
    return not any(word in note for word in other_types)


def link_dividend_6k_evidence(all_groups):
    """精确关联「已正式分红」与同日的分红提示 6-K。

    匹配键只有 ticker + 分红 declaration_date == SEC filing date，并且分红
    必须已有宣告日/官方源/交叉源且无未解决冲突。命中后只把 SEC URL
    加入分红的证据链，不合并 event group；独立 filing review 标记为
    ``linked`` 并从 CA 执行流抑制。
    """
    linked = {}
    for ticker, groups in all_groups.items():
        dividends_by_decl = {}
        for dividend in groups:
            if dividend.etype != "dividend" or R.is_disputed(dividend):
                continue
            decl = R.pick_value(dividend.by_source, "declaration_date")
            formal = bool(
                decl and (
                    R.has_official_source(dividend.by_source)
                    or dividend.status == "confirmed"
                    or len(dividend.by_source) >= 2
                    or bool(decl)
                )
            )
            if formal:
                dividends_by_decl.setdefault(decl, []).append(dividend)

        for filing in groups:
            if (not _suspected_dividend_filing(filing)
                    or getattr(filing, "filing_resolution_source", "") == "operator"):
                continue
            matches = dividends_by_decl.get(filing.anchor_date, [])
            # 多条分红共用宣告日时无法唯一定位，fail closed 继续人工核实。
            if len(matches) != 1:
                continue
            dividend = matches[0]
            sec = (filing.by_source or {}).get("SEC") or {}
            sec_url = sec.get("url") or ""
            if not sec_url:
                continue
            filing_id = getattr(filing, "event_id", "") or sig(filing)
            dividend_id = getattr(dividend, "event_id", "") or sig(dividend)
            filing.event_id = filing_id
            dividend.event_id = dividend_id
            filing.filing_resolution_status = "linked"
            filing.filing_resolution_source = "auto_dividend_match"
            filing.linked_event_id = dividend_id
            dividend.linked_sec_url = sec_url
            dividend.linked_sec_form = sec.get("form") or "6-K"
            dividend.linked_filing_event_id = filing_id
            linked[filing_id] = dividend_id
    return linked


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
    # Bot 写回的 filing 结论与自动分红证据关联必须在产品动作判定前生效。
    # 先分配完整事件 ID；人工结论优先，自动关联不得覆盖操作员结论。
    for _groups in all_groups.values():
        for _g in _groups:
            _g.event_id = sig(_g)
    filing_resolutions = load_filing_review_resolutions()
    apply_filing_review_resolutions(all_groups, filing_resolutions)
    link_dividend_6k_evidence(all_groups)
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
    filing_state = state["filing_relevance_status"]

    # filing 从 ticker+date 升级为带文件指纹的 ID。首次上线时把当前缓存里已经
    # 见过的文件继承旧 seen，避免签名升级导致历史 8-K 整批重放；之后同日出现
    # 新 URL/表格会得到新 ID 并正常首报。
    if state.get("filing_signature_version", 1) < 2:
        for _groups in all_groups.values():
            for _g in _groups:
                if _g.etype == "filing" and legacy_sig(_g) in seen:
                    seen.setdefault(sig(_g), seen[legacy_sig(_g)])
        state["filing_signature_version"] = 2
    today_d = business_today()
    today = today_d.isoformat()
    cutoff30 = (today_d - dt.timedelta(days=30)).isoformat()
    new_events, round_alerts, conflicts, gaps, pending, announced = [], [], [], [], [], []
    forecasts, forecast_updates, contract_updates, filing_updates = [], [], [], []
    forecast_sigs, matched_watch_keys = set(), set()

    # 新标的首次纳入监控时是否静默建基线(见 config.BASELINE_NEW_TICKERS):
    #   开 → 把它的历史事件记为「已见」但不推「新发现」,避免上新一批标的时刷屏
    #   关(默认)→ 照常推,能一次看全新标的的存量事件
    known_tickers = {s.split("|", 1)[0] for s in seen}

    current_filing_ids = set()
    for tk, groups in all_groups.items():
        first_time_ticker = getattr(C, "BASELINE_NEW_TICKERS", False) and tk not in known_tickers
        for g in groups:
            s = sig(g)
            g.event_id = s
            if g.etype == "filing":
                current_filing_ids.add(s)
            filing_update = track_filing_relevance(g, s, filing_state, today)
            if filing_update:
                filing_updates.append(filing_update)
            if s not in seen:
                seen[s] = today
                if not first_time_ticker:
                    new_events.append(g)
            # 普通 SEC 备案仅保留在原文表，不应进入公司行动的
            # 数据异常队列，否则即使日历/推送做了过滤，review 计数
            # 仍会把「财报/高管变动」算成待处理公司行动。
            is_routine_filing = _is_routine_filing(g)
            if g.conflicts and not is_routine_filing:
                conflicts.append(g)
            if g.gaps and not is_routine_filing:
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

            if g.is_future and g.days_to is not None and not _is_routine_filing(g):
                # 持续展示所有正式未来公司行动；普通 SEC 备案只留在原文表，
                # 结构性/未知公司行动则按中央产品结论进入执行或核验节奏。
                _decl = _pk("declaration_date")
                # 正式跟踪判定：已逐项核验的 CompanyIR、已取得宣告日，或 ≥2 源一致。
                # 普通单源且无宣告日仍是预测，只能进入核验提醒，不能让运营执行。
                _confirmed = (g.etype == "filing" or _official or bool(_decl) or
                              len(g.by_source) >= 2)
                event = {"ticker": g.ticker, "etype": g.etype, "date": g.anchor_date,
                         "note": g.note,
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

    # filing 的事件日一过，普通 future/cadence 分支不会再命中。只要条款仍未
    # 被确认或排除，就从持久化快照继续每天发一次核验提醒；最终状态迁移会在
    # 上面的 tracker 中取代本提醒，并且同一业务日只出现一次。
    # 缓存中已不再出现的文件仍可由 Bot 按稳定 ID 结案；先应用该结论，
    # 再生成逾期提醒，以免同一轮既报「已结论」又报「待核实」。满 30 天只
    # 停止每日提醒，不代表普通备案或无需操作。
    filing_updates.extend(apply_missing_filing_resolutions(
        filing_state, filing_resolutions, current_filing_ids, today,
    ))
    filing_updates.extend(collect_overdue_filing_reviews(filing_state, today))

    # 手工观察是一等预测事项：即使供应商当前完全没有对应 event group，也要在
    # Pages/Bot 中可见、按 30/14 天节奏核验，并在预计日过后主动失效；不能成为
    # 写入成功却永远看不见的“黑洞记录”。
    for watch in watches:
        wk = forecast_key(watch)
        if wk in matched_watch_keys or watch.get("status", "watching") != "watching":
            continue
        prev = forecast_state.get(wk) or {}
        watch_date = watch.get("date", "")
        if watch_date < today and prev.get("status") != "expired":
            forecast_updates.append({"ticker": watch.get("ticker"), "etype": watch.get("etype"),
                                     "date": watch_date, "event_id": wk, "kind": "expired",
                                     "verification_kind": "forecast",
                                     "watching": True, "watch_note": watch.get("note", ""),
                                     "srcs": [], "products": C.product_tags(watch.get("ticker", ""))})
            forecast_state[wk] = {"status": "expired", "date": watch_date, "last_seen": today,
                                  "ticker": watch.get("ticker"), "etype": watch.get("etype")}
            continue
        if watch_date < today:
            continue
        try:
            watch_days = (dt.date.fromisoformat(watch_date) - today_d).days
        except (TypeError, ValueError):
            continue
        ticker = watch.get("ticker", "")
        etype = watch.get("etype") or "dividend"
        decision = CP.evaluate(
            ticker, etype, value_verified=False, forecast=True, today=today_d,
        )
        manual_event = {
            "ticker": ticker, "etype": etype, "date": watch_date,
            "event_id": wk, "days": watch_days, "status": "single",
            "decl": None, "record": None, "pay": None,
            "amount": None, "ratio": None, "subtype": "",
            "event_label": CP.event_label(etype), "amount_currency": "",
            "amount_unit": "", "value_display": "", "value_verified": False,
            "amt_srcs": 0, "acked": False, "official": False,
            "first": watch.get("at") or today, "confirmed": False,
            "forecast": True, "manual_watch": True, "srcs": [],
            "products": C.product_tags(ticker), "contract_action": decision,
            "follow_up_mode": "verification", "reminder_state_suffix": "verification",
            "verification_kind": "forecast",
            "filing_relevant": None,
            "risk": C.risk_note(ticker, etype, decision, forecast=True),
            "watching": True, "watch_note": watch.get("note", ""),
        }
        forecasts.append(manual_event)
        forecast_sigs.add(wk)
        forecast_state[wk] = {
            "status": "watching", "date": watch_date, "amount": None,
            "last_seen": today, "ticker": ticker, "etype": etype, "automatic": False,
        }
        reminder = schedule_event_reminder(manual_event, wk, fired, today)
        if reminder:
            round_alerts.append(reminder)

    # 统一「首发日」:分红宣告日(declaration date)→ 否则监控首次发现日
    for tk, groups in all_groups.items():
        for g in groups:
            g.forecast = sig(g) in forecast_sigs
            decl = R.pick_value(g.by_source, "declaration_date")
            g.first_announced = decl or seen.get(sig(g))

    cutoff = (today_d - dt.timedelta(days=30)).isoformat()
    new_events = [g for g in new_events
                  if (g.anchor_date or "") >= cutoff and sig(g) not in forecast_sigs
                  and not _is_routine_filing(g)]
    new_events.sort(key=lambda g: g.anchor_date or "", reverse=True)
    round_alerts.sort(key=lambda x: x["days"])
    conflicts.sort(key=lambda g: g.anchor_date or "", reverse=True)
    gaps.sort(key=lambda g: g.anchor_date or "", reverse=True)
    pending.sort(key=lambda x: x["days"])
    announced.sort(key=lambda x: x.get("decl") or "", reverse=True)
    forecasts.sort(key=lambda x: x["days"])
    filing_updates.sort(key=lambda x: (x.get("transition_date") or "", x.get("ticker") or ""),
                        reverse=True)

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
                                 "value": _ack_display_value(a, g.etype),
                                 "by": a.get("by"), "at": a.get("at"),
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
                                     "value": _ack_display_value(a, g.etype),
                                     "by": a.get("by"), "at": a.get("at"),
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
            ack = _ack_match(
                acks, e.get("ticker"), e.get("etype"), e.get("date")
            )
            if ack:
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
                e.get("event_id") or f"{e.get('ticker')}|{e.get('etype')}|{e.get('date')}"
            )
            if disputed:
                e["disputed"] = True
                e["dispute_vals"] = disputed["vals"]
                e["dispute_detail"] = disputed["detail"]
                e["amount"] = None
                e["ratio"] = None
                e["value_display"] = ""

    _apply_display_value_contract(
        pending + forecasts + forecast_updates + contract_updates + filing_updates
        + round_alerts + announced
    )

    # 所有展示面共用同一份引用契约：先把 event group 和每类 dict 都补全，
    # 避免「单标的卡/推送/网页」各自回退到不同链接。
    for groups in all_groups.values():
        for g in groups:
            attach_event_references(g, refs_config, sec8k_index)
    for e in (pending + forecasts + forecast_updates + contract_updates + filing_updates
              + round_alerts + announced):
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
              "contract_updates": contract_updates, "filing_updates": filing_updates,
              "review": review_summary}
    meta = {"generated": _now_label(), "business_date": today, **_provenance_meta()}

    # 生产采用原子发布：先确认 Lark 已送达（或本地合法静默）并保存去重状态，
    # 再生成可发布站点。投递失败会抛错，旧 Pages 保持不变，避免网站与群提醒
    # 显示两个不同批次的事实。
    sent, delivery_info = deliver_then_save(alerts, meta, state)
    meta["delivery_status"] = "sent" if sent else "legal_skip"
    meta["delivery_info"] = delivery_info

    # 单页站点:日历 + 预警面板(标签切换)
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(RP.build_site(all_groups, source_health, alerts, meta))
    digest = RP.build_text_digest(alerts, meta)
    with open(OUT_DIGEST, "w", encoding="utf-8") as f:
        f.write(digest)

    # 月历事件(供交互机器人画当月月历):近 45 天~未来 80 天内的分红/拆股/并购退市
    cal_lo = (today_d - dt.timedelta(days=45)).isoformat()
    cal_hi = (today_d + dt.timedelta(days=80)).isoformat()
    forecast_event_keys = {
        x.get("event_id") or f"{x.get('ticker')}|{x.get('etype')}|{x.get('date')}"
        for x in forecasts
    }
    calendar_events = []
    for tk, groups in all_groups.items():
        for g in groups:
            ad = g.anchor_date or ""
            if not (cal_lo <= ad <= cal_hi):
                continue
            if g.etype == "filing":
                # 只过滤中央判定已明确是普通备案的记录。Alpaca/FINX 的英文
                # merger/spin_off/name_change 及未知结构性行动必须持续留在日历供核验。
                if _is_routine_filing(g):
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
                     "forecast": sig(g) in forecast_event_keys,
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
        **meta,
        "changelog": load_changelog(),
        "coverage": coverage,
        "counts": {"pending": len(pending), "forecasts": len(forecasts), "new": len(new_events),
                   "conflicts": len(conflicts), "gaps": len(gaps),
                   "announced": len(announced), "filing_updates": len(filing_updates)},
        "announced": announced,
        "recent_declares": recent_declares,
        # Pages 是公开数据；保留业务结果与时间，但不发布确认人的 open_id。
        "resolved": [
            {key: value for key, value in item.items() if key not in ("by", "by_name")}
            for item in resolved
        ],
        "refs": ir_map,
        "ticker_aliases": ticker_aliases,
        "pending": pending,
        "forecasts": forecasts,
        "forecast_updates": forecast_updates,
        "contract_updates": contract_updates,
        "filing_updates": filing_updates,
        "new": [_grp_brief(g) for g in new_events],
        "conflicts": [_grp_brief(g) for g in conflicts],
        "gaps": [_grp_brief(g) for g in gaps],
        "calendar": calendar_events,
    }
    with open(OUT_SITEDATA, "w", encoding="utf-8") as f:
        json.dump(site_data, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 50 + "\n" + digest + "\n" + "=" * 50)
    print(f"\n站点(日历+面板): {OUT_HTML}\nDigest: {OUT_DIGEST}")

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
