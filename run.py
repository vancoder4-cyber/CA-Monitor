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
import sources as S
import reconcile as R
import report as RP
import notify_lark

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")


def _now_label():
    """带时区标注的生成时间:美东(ET) + 北京。GitHub 服务器是 UTC,直接 now() 会显示 UTC 造成误解。"""
    now_utc = dt.datetime.now(dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        et = now_utc.astimezone(ZoneInfo("America/New_York"))
        bj = now_utc.astimezone(ZoneInfo("Asia/Shanghai"))
    except Exception:
        # 退化:无 tzdata 时按夏令时 EDT(-4)/ 北京(+8)近似
        et = now_utc.astimezone(dt.timezone(dt.timedelta(hours=-4)))
        bj = now_utc.astimezone(dt.timezone(dt.timedelta(hours=8)))
    return f"{et.strftime('%Y-%m-%d %H:%M')} ET / {bj.strftime('%H:%M')} 北京"
CACHE = os.path.join(DATA, "cache")
os.makedirs(CACHE, exist_ok=True)
STATE_PATH = os.path.join(DATA, "state.json")
OUT_HTML = os.path.join(HERE, "dashboard.html")
OUT_DIGEST = os.path.join(DATA, "latest_digest.txt")
OUT_SITEDATA = os.path.join(HERE, "site_data.json")  # 供交互机器人读取(会发布到 Pages/data.json)


def load_changelog():
    """解析 CHANGELOG.md -> [{head, items:[...]}, ...](最新在前)。"""
    path = os.path.join(HERE, "CHANGELOG.md")
    if not os.path.exists(path):
        return []
    entries, cur = [], None
    for line in open(path, encoding="utf-8"):
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


def load_refs():
    """读取参考链接维护台 refs.json(每标的 IR 分红页等)。"""
    p = os.path.join(HERE, "refs.json")
    if not os.path.exists(p):
        return {}
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


def _ack_match(acks, ticker, date):
    """找到匹配的确认条目(同标的;确认未记日期则不限日期)。"""
    for a in acks:
        if a.get("ticker") == ticker and (not a.get("date") or a.get("date") == date):
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
    匹配不到返回 ''(前端回退 IR / Nasdaq)。宁可少挂,也不挂错。"""
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


def _grp_brief(g):
    u = (g.by_source.get("SEC") or {}).get("url", "") if g.etype == "filing" else ""
    amt = next((v.get("amount") for v in g.by_source.values() if v.get("amount") is not None), None)
    ratio = next((v.get("ratio") for v in g.by_source.values() if v.get("ratio")), None)
    # src_url:只用 SEC 源给出的**真实 filing url**(并购/退市那类,可靠)。
    # 分红/拆股不再用 EFTS 全文猜——它会命中章程/发债8-K/港交所月报等任何提到 dividend 的文件,
    # 且 ADR 的 USD/ADR 值本就不在 SEC(公司报本币,存托行折 USD)。宁可回退公司IR/备案列表,也不给错的。
    return {"ticker": g.ticker, "etype": g.etype, "date": g.anchor_date,
            "note": g.note, "amount": amt, "ratio": ratio, "sec_url": u, "src_url": u,
            "conflicts": g.conflicts, "gaps": g.gaps}


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
            st.setdefault("seen", {})
            st.setdefault("fired_rounds", {})
            st.setdefault("declared", {})   # sig -> 已推送过的宣告日
            return st
    return {"seen": {}, "fired_rounds": {}, "declared": {}}


def save_state(st):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


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
               "health": health, "groups": [g.to_dict() for g in groups]}
    with open(os.path.join(CACHE, f"{tk}.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return tk, len(groups), health


def fetch(tickers, workers=8, av_limit=24):
    keys = C.get_keys()
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


# ---------------- BUILD ----------------
def build():
    all_groups, source_health = {}, {}
    for tk in C.TICKERS:
        p = os.path.join(CACHE, f"{tk}.json")
        if not os.path.exists(p):
            continue
        d = json.load(open(p, encoding="utf-8"))
        all_groups[tk] = [R.EventGroup.from_dict(x) for x in d["groups"]]
        source_health[tk] = d["health"]

    # 人工确认:给「所有」匹配的事件组打 acked —— 不只是冲突,单源事件也要能被人工放行
    acks = load_acknowledged()
    if acks:
        for _tk, _gs in all_groups.items():
            for _g in _gs:
                _a = _ack_match(acks, _g.ticker, _g.anchor_date)
                if _a:
                    _g.acked = True
                    try:
                        _g.ack_value = float(_a["value"]) if _a.get("value") not in (None, "") else None
                    except Exception:
                        _g.ack_value = None

    state = load_state()
    seen, fired, declared = state["seen"], state["fired_rounds"], state["declared"]
    today = dt.date.today().isoformat()
    cutoff30 = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    new_events, round_alerts, conflicts, gaps, pending, announced = [], [], [], [], [], []

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
                                      "amount": _pk("amount"), "ratio": _pk("ratio"), "amt_srcs": _amt_srcs,
                                      "acked": getattr(g, "acked", False),
                                      "products": C.product_tags(g.ticker)})

            if g.is_future and g.etype != "filing" and g.days_to is not None:
                # 持续推送:所有"已公告未执行"事件,每次跑都列出(带 D-天数 + 产品 + 风控提示)
                _decl = _pk("declaration_date")
                # 「真·已宣告」判定:有宣告日,或 ≥2 源都有这个事件。
                # 否则多半是单源(Alpaca)按节奏推的**预估**,公司并未实际公告 —— 不能当已公告让运营去准备。
                _confirmed = bool(_decl) or len(g.by_source) >= 2
                pending.append({"ticker": g.ticker, "etype": g.etype, "date": g.anchor_date,
                                "days": g.days_to, "status": g.status,
                                "decl": _decl, "record": _pk("record_date"),
                                "pay": _pk("pay_date"), "amount": _pk("amount"), "ratio": _pk("ratio"),
                                "amt_srcs": _amt_srcs, "acked": getattr(g, "acked", False),
                                "first": _decl or seen.get(s),
                                "confirmed": _confirmed, "srcs": sorted(g.by_source.keys()),
                                "products": C.product_tags(g.ticker), "risk": C.risk_note(g.ticker, g.etype)})
                done = set(fired.get(s, []))
                # 只触发「最接近的一轮」:跨过的更大轮次一并标记,避免补推一堆
                cands = [r for r in C.ALERT_ROUNDS if r >= g.days_to and r not in done]
                if cands:
                    rnd = min(cands)
                    ops, risk_copy = C.round_copy(rnd)
                    round_alerts.append({"ticker": g.ticker, "etype": g.etype,
                                         "date": g.anchor_date, "days": g.days_to, "round": rnd,
                                         "decl": _pk("declaration_date"), "record": _pk("record_date"),
                                         "pay": _pk("pay_date"), "amount": _pk("amount"),
                                         "ratio": _pk("ratio"), "products": C.product_tags(g.ticker),
                                         "ops": ops, "risk_copy": risk_copy})
                    done |= {r for r in C.ALERT_ROUNDS if r >= g.days_to}
                fired[s] = sorted(done, reverse=True)

    # 统一「首发日」:分红宣告日(declaration date)→ 否则监控首次发现日
    for tk, groups in all_groups.items():
        for g in groups:
            decl = next((v.get("declaration_date") for v in g.by_source.values()
                         if v.get("declaration_date")), None)
            g.first_announced = decl or seen.get(sig(g))

    cutoff = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    new_events = [g for g in new_events if (g.anchor_date or "") >= cutoff]
    new_events.sort(key=lambda g: g.anchor_date or "", reverse=True)
    round_alerts.sort(key=lambda x: x["days"])
    conflicts.sort(key=lambda g: g.anchor_date or "", reverse=True)
    gaps.sort(key=lambda g: g.anchor_date or "", reverse=True)
    pending.sort(key=lambda x: x["days"])
    announced.sort(key=lambda x: x.get("decl") or "", reverse=True)

    # 人工确认:把已确认的冲突/空缺从报警里剔除(停推+网页 finalize),记入 resolved
    # 确认 = 人工已核实该事件 → 冲突和空缺**一起**消(和机器人 apply_acks 口径一致);
    # 否则会出现"值确认了、空缺还挂着"的怪象(NOK/SONY 就是)。
    resolved = []
    if acks:
        _res_seen = set()
        _active = []
        for g in conflicts:
            a = _ack_match(acks, g.ticker, g.anchor_date)
            if a:
                resolved.append({"ticker": g.ticker, "etype": g.etype, "date": g.anchor_date,
                                 "value": a.get("value"), "by": a.get("by"), "at": a.get("at"),
                                 "detail": "; ".join(g.conflicts)})
                _res_seen.add((g.ticker, g.anchor_date))
            else:
                _active.append(g)
        conflicts = _active
        _active_gaps = []
        for g in gaps:
            a = _ack_match(acks, g.ticker, g.anchor_date)
            if a:
                if (g.ticker, g.anchor_date) not in _res_seen:
                    resolved.append({"ticker": g.ticker, "etype": g.etype, "date": g.anchor_date,
                                     "value": a.get("value"), "by": a.get("by"), "at": a.get("at"),
                                     "detail": "; ".join(g.gaps)})
                    _res_seen.add((g.ticker, g.anchor_date))
            else:
                _active_gaps.append(g)
        gaps = _active_gaps

    # 给「待执行」分红预挂核对链接(供网页预警面板显示):8-K(Item8.01) / IR(refs) / 否则前端回退 Nasdaq
    _ir_map = load_refs().get("ir_dividend", {})
    _sec8k = build_sec8k_index(all_groups)
    for e in pending:
        if e.get("etype") == "dividend":
            e["decl_url"] = match_decl_8k(_sec8k, e["ticker"], e.get("decl"))
            e["ir_url"] = _ir_map.get(e["ticker"], "")

    # ---- 人工介入闭环:每条异常挂多久没人确认(不豁免、不自动消失、每次跑都重报)----
    # 异常 = 字段冲突 / 数据空缺 / 待执行里「未见宣告日的单源预估」。
    # 唯一消解方式:群里发「确认 代码 [正确值]」。挂越久越显眼,超 REVIEW_ESCALATE_DAYS 天在推送里 @ 人升级。
    review = state.setdefault("review", {})
    today_d = dt.date.today()

    def _age(key):
        first = review.get(key)
        if not first:
            review[key] = today
            return 0
        try:
            return (today_d - dt.date.fromisoformat(first)).days
        except Exception:
            return 0

    unconfirmed = [x for x in pending if not x.get("confirmed", True)]
    open_keys = set()
    for g in conflicts:
        k = f"{sig(g)}#conflict"; open_keys.add(k); g.age_days = _age(k)
    for g in gaps:
        k = f"{sig(g)}#gap"; open_keys.add(k); g.age_days = _age(k)
    for x in unconfirmed:
        k = f"{x['ticker']}|{x['etype']}|{x['date']}#unconfirmed"
        open_keys.add(k); x["age_days"] = _age(k)
    # 已被人工确认 / 事件已消失的,从待办里清掉(只有这两种情况才会消失)
    for k in list(review):
        if k not in open_keys:
            review.pop(k, None)

    ages = ([g.age_days for g in conflicts] + [g.age_days for g in gaps]
            + [x["age_days"] for x in unconfirmed])
    esc = C.REVIEW_ESCALATE_DAYS
    review_summary = {"open": len(open_keys), "overdue": sum(1 for a in ages if a >= esc),
                      "max_age": max(ages) if ages else 0, "escalate_days": esc,
                      "conflicts": len(conflicts), "gaps": len(gaps),
                      "unconfirmed": len(unconfirmed)}

    alerts = {"new": new_events, "rounds": round_alerts, "conflicts": conflicts,
              "gaps": gaps, "pending": pending, "announced": announced, "resolved": resolved,
              "review": review_summary}
    meta = {"generated": _now_label()}

    # 单页站点:日历 + 预警面板(标签切换)
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(RP.build_site(all_groups, source_health, alerts, meta))
    digest = RP.build_text_digest(alerts, meta)
    with open(OUT_DIGEST, "w", encoding="utf-8") as f:
        f.write(digest)

    # 月历事件(供交互机器人画当月月历):近 45 天~未来 80 天内的分红/拆股/并购退市
    cal_lo = (dt.date.today() - dt.timedelta(days=45)).isoformat()
    cal_hi = (dt.date.today() + dt.timedelta(days=80)).isoformat()
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
            calendar_events.append({"ticker": g.ticker, "etype": g.etype, "date": ad,
                                    "amount": _ck("amount"), "ratio": _ck("ratio"), "note": g.note,
                                    "amt_srcs": max(R.n_src(g.by_source, "amount"), R.n_src(g.by_source, "ratio")),
                                    "acked": getattr(g, "acked", False),
                                    "record": _ck("record_date"), "pay": _ck("pay_date"),
                                    "decl": _ck("declaration_date"),
                                    "first": getattr(g, "first_announced", None),
                                    "status": g.status, "risk": C.risk_note(g.ticker, g.etype),
                                    "url": (g.by_source.get("SEC") or {}).get("url", "") if g.etype == "filing" else "",
                                    "products": C.product_tags(g.ticker)})

    # 人工确认带「正确值」时,用确认值覆盖该标的事件的金额(网页/卡片显示 finalize 后的值)
    for a in acks:
        if a.get("value") in (None, ""):
            continue
        try:
            v = float(a["value"])
        except Exception:
            continue
        for e in calendar_events + pending:
            if e["ticker"] == a["ticker"] and (not a.get("date") or e.get("date") == a.get("date")):
                if e.get("etype") == "dividend":
                    e["amount"] = v

    # ---- 金额门禁:仍有「未确认冲突」的事件,金额/比例不是权威值,三面都不得当确定值展示 ----
    # 人已确认的不在 conflicts 里(上面已用确认值覆盖),所以不会被 gate —— 只有没人确认的才封。
    # 目的:防止运营照着一个各源都对不上、还没人核过的数字去执行。
    _disputed = {}
    for g in conflicts:
        vals = {}
        for s, v in g.by_source.items():
            x = v.get("amount") if v.get("amount") is not None else v.get("ratio")
            if x is not None:
                vals[s] = x
        _disputed[sig(g)] = {"detail": "; ".join(g.conflicts), "vals": vals}
    for e in calendar_events + pending + announced:
        d = _disputed.get(f"{e['ticker']}|{e['etype']}|{e.get('date')}")
        if d:
            e["disputed"] = True
            e["dispute_vals"] = d["vals"]
            e["dispute_detail"] = d["detail"]

    # 资产覆盖(现货/合约 × 标的类型 × 是否监控)
    TYPE_CN = {"equity": "个股", "etf": "ETF", "commodity": "商品/外汇", "foreign": "海外股"}
    coverage = []
    for tk in C.ALL_ASSETS:
        coverage.append({"ticker": tk, "name": C.NAMES.get(tk, ""),
                         "spot": tk in C.SPOT_TICKERS, "contract": tk in C.CONTRACT_TICKERS,
                         "type": C.asset_type(tk), "type_cn": TYPE_CN.get(C.asset_type(tk), C.asset_type(tk)),
                         "monitored": C.is_monitored(tk)})

    # 最近宣告(declaration)的事件:取最新 5 个,已派发完的标 ended
    today_iso = dt.date.today().isoformat()
    recent_declares = []
    for tk, groups in all_groups.items():
        for g in groups:
            decl = next((v.get("declaration_date") for v in g.by_source.values()
                         if v.get("declaration_date")), None)
            if not decl:
                continue
            def _dk(f, _g=g):
                return next((v.get(f) for v in _g.by_source.values() if v.get(f)), None)
            pay = _dk("pay_date")
            end_date = pay or g.anchor_date or ""
            ended = bool(end_date) and end_date < today_iso
            try:
                days = (dt.date.fromisoformat(g.anchor_date) - dt.date.today()).days if g.anchor_date else None
            except Exception:
                days = None
            recent_declares.append({"ticker": g.ticker, "etype": g.etype, "date": g.anchor_date,
                                    "decl": decl, "record": _dk("record_date"), "pay": pay,
                                    "amount": _dk("amount"), "ratio": _dk("ratio"),
                                    "days": days, "ended": ended,
                                    "products": C.product_tags(g.ticker)})
    recent_declares.sort(key=lambda x: x.get("decl") or "", reverse=True)
    recent_declares = recent_declares[:5]

    # 分红 → 宣告 8-K 精确匹配:给每条分红挂上那份 8-K 的 SEC 链接(匹配不到则为空,前端回退 Nasdaq)
    ir_map = load_refs().get("ir_dividend", {})
    sec8k = build_sec8k_index(all_groups)
    for lst in (calendar_events, recent_declares):  # pending 已在 build_site 前预挂
        for e in lst:
            if e.get("etype") == "dividend":
                e["decl_url"] = match_decl_8k(sec8k, e["ticker"], e.get("decl"))
                e["ir_url"] = ir_map.get(e["ticker"], "")
    for g in conflicts:  # 冲突组(供 notify_lark 推送用)
        if g.etype == "dividend":
            decl = next((v.get("declaration_date") for v in g.by_source.values() if v.get("declaration_date")), None)
            g.decl_url = match_decl_8k(sec8k, g.ticker, decl)
            g.ir_url = ir_map.get(g.ticker, "")

    # 发布给交互机器人读取的数据(随 Pages 一起部署为 data.json)
    site_data = {
        "generated": meta["generated"],
        "changelog": load_changelog(),
        "coverage": coverage,
        "counts": {"pending": len(pending), "new": len(new_events),
                   "conflicts": len(conflicts), "gaps": len(gaps),
                   "announced": len(announced)},
        "announced": announced,
        "recent_declares": recent_declares,
        "resolved": resolved,
        "refs": ir_map,
        "pending": pending,
        "new": [_grp_brief(g) for g in new_events],
        "conflicts": [_grp_brief(g) for g in conflicts],
        "gaps": [_grp_brief(g) for g in gaps],
        "calendar": calendar_events,
    }
    with open(OUT_SITEDATA, "w", encoding="utf-8") as f:
        json.dump(site_data, f, ensure_ascii=False, indent=2)

    save_state(state)

    print("\n" + "=" * 50 + "\n" + digest + "\n" + "=" * 50)
    print(f"\n站点(日历+面板): {OUT_HTML}\nDigest: {OUT_DIGEST}")

    # 推送到 Lark(未配置则自动跳过)
    sent, info = notify_lark.notify(alerts, meta)
    print(f"Lark: {info}")
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
