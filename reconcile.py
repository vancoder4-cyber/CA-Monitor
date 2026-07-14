# -*- coding: utf-8 -*-
"""归一化与交叉核对引擎。

输入: 每只票的多源 SourceResult
输出: 合并后的事件组列表,每组带状态与报警
  状态: confirmed(≥2源一致) / single(单源待核实) / conflict(≥2源字段不一致)
  报警: gap(已发生事件某源缺失) / conflict(字段不一致)
"""
import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from typing import List, Dict
import config as C


TODAY = dt.date.today()


# ---- 取值的唯一真相(所有消费端都必须走这里,别再各写一份)----
def pick_value(by_source, field):
    """多数票 + 源优先级取值。要的是「公司实际宣告的原值」。

    绝不能用「第一个源赢」——源的顺序不代表谁对,会踩三个坑:
      1) yfinance 按拆股回溯调整历史分红(KLAC 10:1 后把 2.3 报成 0.23)
      2) yfinance 四舍五入到 3 位(0.2475 → 0.248)
      3) Alpaca 对 ADR 报的是扣预扣税后的净额(ASML=gross×0.85;TSM 台湾21%)
    """
    vals = [(s, v.get(field)) for s, v in by_source.items() if v.get(field) is not None]
    if not vals:
        return None
    cnt = Counter(v for _, v in vals)
    top = cnt.most_common(1)[0][1]
    winners = [v for v, n in cnt.items() if n == top]
    if len(winners) == 1:
        return winners[0]
    for s in getattr(C, "SRC_PRIORITY", []):
        for src, v in vals:
            if src == s and v in winners:
                return v
    return vals[0][1]


def n_src(by_source, field):
    """有几个源报了这个字段(<2 = 没交叉验证过,不能当权威值)。"""
    return sum(1 for v in by_source.values() if v.get(field) is not None)


def is_disputed(g):
    """该事件是否还有「未经人工确认」的冲突(run.py 会给已确认的打 acked=True)。"""
    return bool(g.conflicts) and not getattr(g, "acked", False)


def _d(s):
    try:
        return dt.date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


RECON_CUTOFF = (TODAY - dt.timedelta(days=C.LOOKBACK_DAYS)).isoformat()


@dataclass
class EventGroup:
    ticker: str
    etype: str
    anchor_date: str               # 代表日期(除权/生效/filing 日)
    by_source: Dict[str, dict] = field(default_factory=dict)  # source -> 字段
    sources_ok: List[str] = field(default_factory=list)       # 对该票该类返回 ok 的源
    status: str = "single"         # confirmed | single | conflict
    conflicts: List[str] = field(default_factory=list)        # 冲突描述
    gaps: List[str] = field(default_factory=list)             # 空缺描述
    note: str = ""

    @property
    def is_future(self):
        d = _d(self.anchor_date)
        return bool(d and d >= TODAY)

    @property
    def days_to(self):
        d = _d(self.anchor_date)
        return (d - TODAY).days if d else None

    def to_dict(self):
        return {"ticker": self.ticker, "etype": self.etype,
                "anchor_date": self.anchor_date, "by_source": self.by_source,
                "sources_ok": self.sources_ok, "status": self.status,
                "conflicts": self.conflicts, "gaps": self.gaps, "note": self.note}

    @staticmethod
    def from_dict(d):
        return EventGroup(ticker=d["ticker"], etype=d["etype"],
                          anchor_date=d["anchor_date"], by_source=d.get("by_source", {}),
                          sources_ok=d.get("sources_ok", []), status=d.get("status", "single"),
                          conflicts=d.get("conflicts", []), gaps=d.get("gaps", []),
                          note=d.get("note", ""))


def _fields_of(ev, etype):
    if etype == "dividend":
        return {"ex_date": ev.ex_date, "declaration_date": ev.declaration_date,
                "record_date": ev.record_date, "pay_date": ev.pay_date, "amount": ev.amount}
    if etype == "split":
        return {"ex_date": ev.ex_date, "ratio": ev.ratio}
    return {"ex_date": ev.ex_date, "note": ev.note}


def _amount_eq(a, b):
    try:
        return abs(float(a) - float(b)) < 0.0005   # 抹掉浮点噪声,半厘以内视为相同
    except (TypeError, ValueError):
        return a == b


def reconcile_ticker(results) -> List[EventGroup]:
    """results: List[SourceResult] for one ticker."""
    groups: List[EventGroup] = []

    for etype in ("dividend", "split", "filing"):
        # 哪些源对该票该类是 ok(用于空缺判定:只有"在覆盖"的源缺失才算空缺)
        ok_sources = sorted({r.source for r in results
                             if r.status == "ok" and any(e.etype == etype for e in r.events)})
        # 该类型所有事件按日期排序
        evs = []
        for r in results:
            if r.status != "ok":
                continue
            for e in r.events:
                if e.etype == etype and e.ex_date:
                    evs.append(e)
        evs.sort(key=lambda e: e.ex_date)

        # 按时间窗聚类成"同一事件"
        clusters = []
        for e in evs:
            placed = False
            ed = _d(e.ex_date)
            for cl in clusters:
                cd = _d(cl[0].ex_date)
                if cd and ed and abs((ed - cd).days) <= C.GROUP_WINDOW_DAYS:
                    # 同一源同类不重复并入同簇(避免季度内多次)
                    if e.source not in {x.source for x in cl}:
                        cl.append(e); placed = True; break
            if not placed:
                clusters.append([e])

        for cl in clusters:
            g = EventGroup(ticker=cl[0].ticker, etype=etype,
                           anchor_date=min(x.ex_date for x in cl),
                           sources_ok=ok_sources)
            for e in cl:
                g.by_source[e.source] = _fields_of(e, etype)
                if etype == "filing":
                    g.note = e.note
                    g.by_source[e.source]["url"] = e.raw.get("url", "")
                    g.by_source[e.source]["relevant"] = e.raw.get("relevant", False)
                    g.by_source[e.source]["accepted"] = e.raw.get("accepted", "")
                    g.by_source[e.source]["form"] = e.raw.get("form", "")
                    g.by_source[e.source]["items"] = e.raw.get("items", "")
            _evaluate(g, etype)
            groups.append(g)

    groups.sort(key=lambda g: (g.anchor_date or ""), reverse=True)
    return groups


def _evaluate(g: EventGroup, etype):
    srcs = list(g.by_source.keys())

    # filing 不做跨源核对(EDGAR 唯一权威源)
    if etype == "filing":
        g.status = "confirmed"
        return

    compare_fields = C.DIV_COMPARE_FIELDS if etype == "dividend" else C.SPLIT_COMPARE_FIELDS

    # 只对"近窗口(近 LOOKBACK 天)+ 未来"的事件做报警,避免老历史覆盖深度差异造成噪音
    in_window = (g.anchor_date or "") >= RECON_CUTOFF

    # ---- 字段一致性(零容忍)----
    has_conflict = False
    for fld in compare_fields:
        vals = {s: g.by_source[s].get(fld) for s in srcs if g.by_source[s].get(fld) is not None}
        uniq = list(vals.values())
        if len(uniq) < 2:
            continue
        first = uniq[0]
        same = all(_amount_eq(v, first) if fld == "amount" else v == first for v in uniq)
        if not same:
            has_conflict = True
            if in_window:
                pretty = ", ".join(f"{s}={vals[s]}" for s in vals)
                g.conflicts.append(f"{fld}: {pretty}")

    # ---- 状态 ----
    if has_conflict:
        g.status = "conflict"
    elif len(srcs) >= 2:
        g.status = "confirmed"
    else:
        g.status = "single"

    # ---- 空缺(仅对近窗口内已发生事件:在覆盖该票该类的 ok 源里,谁缺了)----
    if not g.is_future and in_window:
        missing = [s for s in g.sources_ok if s not in g.by_source]
        # 降噪:历史覆盖短的源(如 FINX),仅对近窗口内的事件算空缺;
        # 更早的历史事件这些源没有也不报(它们本就只回近期+未来)。
        if missing:
            sh_cut = (TODAY - dt.timedelta(days=C.SHORT_HISTORY_GAP_DAYS)).isoformat()
            missing = [s for s in missing
                       if s not in C.SHORT_HISTORY_SOURCES or (g.anchor_date or "") >= sh_cut]
        if missing and len(g.by_source) >= 1:
            g.gaps.append(f"{'/'.join(missing)} 缺失此事件(其它源有)")


def summarize(all_groups: Dict[str, List[EventGroup]]):
    """跨全部标的的统计 + 报警清单。"""
    conflicts, gaps = [], []
    for tk, groups in all_groups.items():
        for g in groups:
            if g.conflicts:
                conflicts.append(g)
            if g.gaps:
                gaps.append(g)
    return {"conflicts": conflicts, "gaps": gaps}
