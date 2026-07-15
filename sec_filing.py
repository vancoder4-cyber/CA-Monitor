# -*- coding: utf-8 -*-
"""把一条分红/拆股事件解析到**具体那封 SEC filing**(而不是公司备案列表)。

用 EDGAR 全文检索(EFTS):按 CIK + 关键词 + 表单(8-K 普通股 / 6-K 外国发行人)+ 除息日前的时间窗,
定位最接近的那封宣告 filing,直达文档 URL。取不到就返回 None(前端回退到公司 IR / 备案列表)。

只在流水线(run.py)里跑,每次 build 顺带解析,3×/交易日,不在机器人实时请求。全程 best-effort,
任何异常都吞掉、返回 None —— 绝不影响核对/报警主流程。
"""
import os
import json
import datetime as dt
import urllib.request
import urllib.parse

_UA = {"User-Agent": os.environ.get("SEC_UA", "CA-Monitor vancoder4@gmail.com")}
_EFTS = "https://efts.sec.gov/LATEST/search-index"
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

_CIK = None                 # {TICKER: "0000320193"}
_CACHE = {}                 # (ticker, etype, date) -> url or None
# 关键词按事件类型;命中宣告文里的这些词
_KW = {"dividend": '"dividend"', "split": '"stock split"'}


def _load_ciks():
    global _CIK
    if _CIK is not None:
        return _CIK
    _CIK = {}
    try:
        req = urllib.request.Request(_TICKERS_URL, headers=_UA)
        j = json.load(urllib.request.urlopen(req, timeout=20))
        for _, v in j.items():
            t = str(v.get("ticker", "")).upper()
            if t:
                _CIK[t] = str(v["cik_str"]).zfill(10)
    except Exception as e:
        print("sec_filing: load ciks err:", e)
    return _CIK


def _efts(cik, keyword, startdt, enddt):
    q = urllib.parse.urlencode({
        "q": keyword, "forms": "8-K,6-K", "ciks": cik,
        "startdt": startdt, "enddt": enddt})
    req = urllib.request.Request(f"{_EFTS}?{q}", headers=_UA)
    j = json.load(urllib.request.urlopen(req, timeout=12))
    return j.get("hits", {}).get("hits", [])


def resolve_filing_url(ticker, etype, ex_date):
    """返回该事件宣告 filing 的直达文档 URL;取不到返回 None。best-effort。"""
    # 出问题可一键关闭(设 SEC_FILING=0),流水线立刻回退到公司备案列表,不受影响
    if os.environ.get("SEC_FILING", "1") == "0":
        return None
    if etype not in _KW or not ex_date:
        return None
    key = (ticker, etype, ex_date)
    if key in _CACHE:
        return _CACHE[key]
    url = None
    try:
        cik = _load_ciks().get((ticker or "").upper())
        if cik:
            ex = dt.date.fromisoformat(ex_date)
            # 宣告在除息日**之前**(通常提前 2–8 周);给个宽窗
            start = (ex - dt.timedelta(days=100)).isoformat()
            end = (ex + dt.timedelta(days=5)).isoformat()
            hits = _efts(cik, _KW[etype], start, end)
            best = None  # (排序键, url)
            for h in hits:
                src = h.get("_source", {})
                fd = src.get("file_date", "")
                _id = h.get("_id", "")
                if ":" not in _id or not fd:
                    continue
                adsh, doc = _id.split(":", 1)
                durl = (f"https://www.sec.gov/Archives/edgar/data/"
                        f"{int(cik)}/{adsh.replace('-', '')}/{doc}")
                # 优先"除息日之前、且最接近"的那封;没有更早的再取最接近
                before = fd <= ex_date
                try:
                    dist = abs((dt.date.fromisoformat(fd) - ex).days)
                except Exception:
                    dist = 9999
                rank = (0 if before else 1, dist)   # 先要 before,再要近
                if best is None or rank < best[0]:
                    best = (rank, durl)
            url = best[1] if best else None
    except Exception as e:
        print(f"sec_filing: resolve {ticker}/{etype}/{ex_date} err:", e)
        url = None
    _CACHE[key] = url
    return url
