# -*- coding: utf-8 -*-
"""主流程:抓取 → 核对 → 报警 → 输出面板+digest。

两段式(绕过单次运行时限,也便于调度):
    python run.py fetch [T1 T2 ...]   # 抓取+核对指定票(默认全量),结果缓存到 data/cache/
    python run.py build               # 合并所有缓存 → 计算报警 → 写 dashboard.html + digest
    python run.py                     # = fetch 全量 + build(一次跑完,适合定时任务)

状态文件 data/state.json:已见事件签名(新发现判定)+ 已触发预警轮次(去重)。
"""
import os, sys, json, datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
import config as C
import sources as S
import reconcile as R
import report as RP
import notify_lark

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
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


def _grp_brief(g):
    u = (g.by_source.get("SEC") or {}).get("url", "") if g.etype == "filing" else ""
    amt = next((v.get("amount") for v in g.by_source.values() if v.get("amount") is not None), None)
    ratio = next((v.get("ratio") for v in g.by_source.values() if v.get("ratio")), None)
    return {"ticker": g.ticker, "etype": g.etype, "date": g.anchor_date,
            "note": g.note, "amount": amt, "ratio": ratio, "sec_url": u,
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

    state = load_state()
    seen, fired, declared = state["seen"], state["fired_rounds"], state["declared"]
    today = dt.date.today().isoformat()
    cutoff30 = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    new_events, round_alerts, conflicts, gaps, pending, announced = [], [], [], [], [], []

    for tk, groups in all_groups.items():
        for g in groups:
            s = sig(g)
            if s not in seen:
                new_events.append(g); seen[s] = today
            if g.conflicts:
                conflicts.append(g)
            if g.gaps:
                gaps.append(g)

            def _pk(f, _g=g):
                return next((v.get(f) for v in _g.by_source.values() if v.get(f)), None)

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
                                      "amount": _pk("amount"), "ratio": _pk("ratio"),
                                      "products": C.product_tags(g.ticker)})

            if g.is_future and g.etype != "filing" and g.days_to is not None:
                # 持续推送:所有"已公告未执行"事件,每次跑都列出(带 D-天数 + 产品 + 风控提示)
                pending.append({"ticker": g.ticker, "etype": g.etype, "date": g.anchor_date,
                                "days": g.days_to, "status": g.status,
                                "decl": _pk("declaration_date"), "record": _pk("record_date"),
                                "pay": _pk("pay_date"), "amount": _pk("amount"), "ratio": _pk("ratio"),
                                "first": _pk("declaration_date") or seen.get(s),
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

    alerts = {"new": new_events, "rounds": round_alerts, "conflicts": conflicts,
              "gaps": gaps, "pending": pending, "announced": announced}
    meta = {"generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}

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
                return next((v.get(f) for v in _g.by_source.values() if v.get(f)), None)
            calendar_events.append({"ticker": g.ticker, "etype": g.etype, "date": ad,
                                    "amount": _ck("amount"), "ratio": _ck("ratio"), "note": g.note,
                                    "record": _ck("record_date"), "pay": _ck("pay_date"),
                                    "decl": _ck("declaration_date"),
                                    "first": getattr(g, "first_announced", None),
                                    "status": g.status, "risk": C.risk_note(g.ticker, g.etype),
                                    "url": (g.by_source.get("SEC") or {}).get("url", "") if g.etype == "filing" else "",
                                    "products": C.product_tags(g.ticker)})

    # 资产覆盖(现货/合约 × 标的类型 × 是否监控)
    TYPE_CN = {"equity": "个股", "etf": "ETF", "commodity": "商品/外汇", "foreign": "海外股"}
    coverage = []
    for tk in C.ALL_ASSETS:
        coverage.append({"ticker": tk, "name": C.NAMES.get(tk, ""),
                         "spot": tk in C.SPOT_TICKERS, "contract": tk in C.CONTRACT_TICKERS,
                         "type": C.asset_type(tk), "type_cn": TYPE_CN.get(C.asset_type(tk), C.asset_type(tk)),
                         "monitored": C.is_monitored(tk)})

    # 发布给交互机器人读取的数据(随 Pages 一起部署为 data.json)
    site_data = {
        "generated": meta["generated"],
        "changelog": load_changelog(),
        "coverage": coverage,
        "counts": {"pending": len(pending), "new": len(new_events),
                   "conflicts": len(conflicts), "gaps": len(gaps),
                   "announced": len(announced)},
        "announced": announced,
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
