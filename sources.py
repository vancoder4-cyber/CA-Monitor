# -*- coding: utf-8 -*-
"""五源数据抓取器。

每个抓取器返回 SourceResult,关键设计:
- status="ok"      源正常应答(events 可能为空 = "源说这只票没有此类事件")
- status="unavailable" 源被限流 / 付费墙 / 报错(= "没查到" != "源说没有")
这区分让交叉核对不会把"源不可用"误判成"空缺"。
"""
import os
import json
import time
import datetime as dt
from dataclasses import dataclass, field
from typing import List, Optional

import requests
import config as C


# ---------------- 归一化事件模型 ----------------
@dataclass
class Event:
    ticker: str
    etype: str               # dividend | split | filing
    source: str
    ex_date: Optional[str] = None      # YYYY-MM-DD
    record_date: Optional[str] = None
    pay_date: Optional[str] = None
    declaration_date: Optional[str] = None
    amount: Optional[float] = None      # 分红金额
    ratio: Optional[str] = None         # 拆股比例 "num:den"
    note: str = ""                      # filing 描述等
    raw: dict = field(default_factory=dict)


@dataclass
class SourceResult:
    source: str
    ticker: str
    status: str                  # ok | unavailable
    events: List[Event] = field(default_factory=list)
    detail: str = ""             # 不可用原因


def _f(x):
    try:
        return round(float(x), 6)
    except (TypeError, ValueError):
        return None


def _norm_date(x):
    if not x:
        return None
    s = str(x)[:10]
    return s if len(s) == 10 and s[4] == "-" else None


def _norm_date_us(x):
    """MM/DD/YYYY -> YYYY-MM-DD(Nasdaq 用)。"""
    if not x:
        return None
    s = str(x).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _money(x):
    if x is None:
        return None
    return _f(str(x).replace("$", "").replace(",", "").strip())


_HDR_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json", "Accept-Language": "en-US",
}


def _get_retry(url, headers=None, params=None, timeout=20, tries=3, backoff=1.5):
    """带重试退避的 GET(Nasdaq 等抽风接口用)。"""
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=timeout)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = str(e)
        time.sleep(backoff * (i + 1))
    raise RuntimeError(last or "request failed")


# ---------------- 1) yfinance(Yahoo,免 key)----------------
def fetch_yfinance(ticker: str) -> List[SourceResult]:
    import yfinance as yf
    out = []
    try:
        t = yf.Ticker(ticker)
        divs = t.dividends          # Series index=日期 value=金额(除权日)
        splits = t.splits
    except Exception as e:
        return [SourceResult("yfinance", ticker, "unavailable", detail=f"{e}")]

    # 分红
    dev = []
    try:
        for d, amt in divs.items():
            dev.append(Event(ticker, "dividend", "yfinance",
                             ex_date=_norm_date(d), amount=_f(amt)))
    except Exception as e:
        out.append(SourceResult("yfinance", ticker, "unavailable", detail=f"div:{e}"))
    else:
        out.append(SourceResult("yfinance", ticker, "ok", dev))

    # 拆股
    spl = []
    try:
        for d, f in splits.items():
            spl.append(Event(ticker, "split", "yfinance",
                             ex_date=_norm_date(d), ratio=_ratio_from_float(f)))
    except Exception as e:
        out.append(SourceResult("yfinance", ticker, "unavailable", detail=f"split:{e}"))
    else:
        out.append(SourceResult("yfinance", ticker, "ok", spl))
    return out


def _ratio_from_float(f):
    """yfinance 用浮点表示拆股(4.0=4:1, 0.1=1:10)。转成 num:den。"""
    try:
        f = float(f)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    if f >= 1:
        return f"{int(round(f))}:1"
    return f"1:{int(round(1/f))}"


# ---------------- 2) FMP(stable 接口)----------------
def fetch_fmp(ticker: str, key: str) -> List[SourceResult]:
    out = []
    base = "https://financialmodelingprep.com/stable"
    # 分红
    try:
        r = requests.get(f"{base}/dividends", params={"symbol": ticker, "apikey": key}, timeout=25)
        if r.status_code != 200:
            out.append(SourceResult("FMP", ticker, "unavailable", detail=f"div HTTP {r.status_code}"))
        else:
            data = r.json()
            if isinstance(data, dict) and data.get("Error Message"):
                out.append(SourceResult("FMP", ticker, "unavailable", detail=data["Error Message"][:80]))
            else:
                evs = [Event(ticker, "dividend", "FMP",
                             ex_date=_norm_date(x.get("date")),
                             record_date=_norm_date(x.get("recordDate")),
                             pay_date=_norm_date(x.get("paymentDate")),
                             declaration_date=_norm_date(x.get("declarationDate")),
                             amount=_f(x.get("dividend")), raw=x)
                       for x in data]
                out.append(SourceResult("FMP", ticker, "ok", evs))
    except Exception as e:
        out.append(SourceResult("FMP", ticker, "unavailable", detail=f"div:{e}"))
    # 拆股
    try:
        r = requests.get(f"{base}/splits", params={"symbol": ticker, "apikey": key}, timeout=25)
        if r.status_code != 200:
            out.append(SourceResult("FMP", ticker, "unavailable", detail=f"split HTTP {r.status_code}"))
        else:
            data = r.json()
            if isinstance(data, dict) and data.get("Error Message"):
                out.append(SourceResult("FMP", ticker, "unavailable", detail=data["Error Message"][:80]))
            else:
                evs = []
                for x in data:
                    num, den = x.get("numerator"), x.get("denominator")
                    ratio = f"{int(num)}:{int(den)}" if num and den else None
                    evs.append(Event(ticker, "split", "FMP",
                                     ex_date=_norm_date(x.get("date")), ratio=ratio, raw=x))
                out.append(SourceResult("FMP", ticker, "ok", evs))
    except Exception as e:
        out.append(SourceResult("FMP", ticker, "unavailable", detail=f"split:{e}"))
    return out


# ---------------- 3) Alpha Vantage(免费 25/天,1/秒)----------------
def fetch_alphavantage(ticker: str, key: str, do_splits: bool = True) -> List[SourceResult]:
    out = []

    def _call(func):
        r = requests.get("https://www.alphavantage.co/query",
                         params={"function": func, "symbol": ticker, "apikey": key}, timeout=25)
        j = r.json()
        # 限流/提示信息 → 源不可用
        if any(k in j for k in ("Information", "Note", "Error Message")):
            msg = j.get("Information") or j.get("Note") or j.get("Error Message")
            return None, msg[:100]
        return j, None

    # 分红
    try:
        j, err = _call("DIVIDENDS")
        if err:
            out.append(SourceResult("AlphaVantage", ticker, "unavailable", detail=err))
        else:
            evs = [Event(ticker, "dividend", "AlphaVantage",
                         ex_date=_norm_date(x.get("ex_dividend_date")),
                         record_date=_norm_date(x.get("record_date")),
                         pay_date=_norm_date(x.get("payment_date")),
                         declaration_date=_norm_date(x.get("declaration_date")),
                         amount=_f(x.get("amount")), raw=x)
                   for x in j.get("data", [])]
            out.append(SourceResult("AlphaVantage", ticker, "ok", evs))
    except Exception as e:
        out.append(SourceResult("AlphaVantage", ticker, "unavailable", detail=f"div:{e}"))

    if do_splits:
        time.sleep(1.2)  # 尊重 1/秒
        try:
            j, err = _call("SPLITS")
            if err:
                out.append(SourceResult("AlphaVantage", ticker, "unavailable", detail=err))
            else:
                evs = [Event(ticker, "split", "AlphaVantage",
                             ex_date=_norm_date(x.get("effective_date")),
                             ratio=_av_ratio(x.get("split_factor")), raw=x)
                       for x in j.get("data", [])]
                out.append(SourceResult("AlphaVantage", ticker, "ok", evs))
        except Exception as e:
            out.append(SourceResult("AlphaVantage", ticker, "unavailable", detail=f"split:{e}"))
    return out


def _av_ratio(factor):
    return _ratio_from_float(factor)


# ---------------- 4) SEC EDGAR(并购/退市 filing 信号,免 key)----------------
_CIK_CACHE = {}
_CIK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cik_map.json")

def _load_cik_map():
    global _CIK_CACHE
    if _CIK_CACHE:
        return _CIK_CACHE
    # 磁盘缓存(避免每进程重复下载 ~10MB 大表)
    try:
        if os.path.exists(_CIK_FILE):
            with open(_CIK_FILE, encoding="utf-8") as f:
                _CIK_CACHE = json.load(f)
                if _CIK_CACHE:
                    return _CIK_CACHE
    except Exception:
        pass
    try:
        r = requests.get("https://www.sec.gov/files/company_tickers.json",
                         headers={"User-Agent": C.SEC_UA}, timeout=25)
        for row in r.json().values():
            _CIK_CACHE[row["ticker"].upper()] = str(row["cik_str"]).zfill(10)
        os.makedirs(os.path.dirname(_CIK_FILE), exist_ok=True)
        with open(_CIK_FILE, "w", encoding="utf-8") as f:
            json.dump(_CIK_CACHE, f)
    except Exception:
        pass
    return _CIK_CACHE


def fetch_sec(ticker: str, lookback_days: int) -> SourceResult:
    cik = _load_cik_map().get(ticker.upper())
    if not cik:
        return SourceResult("SEC", ticker, "unavailable", detail="未找到 CIK(可能非美股/未在 EDGAR 登记)")
    try:
        r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                         headers={"User-Agent": C.SEC_UA}, timeout=25)
        if r.status_code != 200:
            return SourceResult("SEC", ticker, "unavailable", detail=f"HTTP {r.status_code}")
        recent = r.json().get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accns = recent.get("accessionNumber", [])
        docs = recent.get("primaryDocument", [])
        items_all = recent.get("items", [])
        accepted_all = recent.get("acceptanceDateTime", [])
        cutoff = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()
        # 非 8-K 的关注表格本身就与公司行动相关
        _form_relevant = {"25", "25-NSE", "425", "S-4", "DEFM14A", "8-K12B", "15-12B",
                          "SC TO-I", "SC 14D9"}
        evs = []
        for i, form in enumerate(forms):
            if form not in C.SEC_FORMS_OF_INTEREST:
                continue
            fdate = dates[i] if i < len(dates) else None
            if fdate and fdate < cutoff:
                continue
            accn = accns[i].replace("-", "") if i < len(accns) else ""
            doc = docs[i] if i < len(docs) else ""
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn}/{doc}" if accn else ""
            items_str = items_all[i] if i < len(items_all) else ""
            accepted = (accepted_all[i] if i < len(accepted_all) else "") or ""
            accepted = accepted.replace("T", " ")[:16]   # 'YYYY-MM-DD HH:MM'
            if form == "8-K":
                descs, relevant = C.describe_8k(items_str)
                note = "8-K · " + ("、".join(descs) if descs else "重大事件")
            else:
                note = f"{form} · {C.SEC_FORMS_OF_INTEREST[form]}"
                relevant = form in _form_relevant
            evs.append(Event(ticker, "filing", "SEC", ex_date=fdate, note=note,
                             raw={"form": form, "url": url, "items": items_str,
                                  "relevant": relevant, "accepted": accepted}))
        return SourceResult("SEC", ticker, "ok", evs)
    except Exception as e:
        return SourceResult("SEC", ticker, "unavailable", detail=f"{e}")


# ---------------- 5) Nasdaq(免 key:按票分红 + 市场拆股日历)----------------
def fetch_nasdaq_dividends(ticker: str) -> SourceResult:
    url = f"https://api.nasdaq.com/api/quote/{ticker}/dividends?assetclass=stocks"
    try:
        r = _get_retry(url, headers=_HDR_BROWSER, timeout=20)
        d = r.json().get("data") or {}
        rows = ((d.get("dividends") or {}).get("rows")) or []
        evs = []
        for x in rows:
            evs.append(Event(ticker, "dividend", "Nasdaq",
                             ex_date=_norm_date_us(x.get("exOrEffDate")),
                             record_date=_norm_date_us(x.get("recordDate")),
                             pay_date=_norm_date_us(x.get("paymentDate")),
                             declaration_date=_norm_date_us(x.get("declarationDate")),
                             amount=_money(x.get("amount")), raw=x))
        return SourceResult("Nasdaq", ticker, "ok", evs)
    except Exception as e:
        return SourceResult("Nasdaq", ticker, "unavailable", detail=f"{e}")


# 市场级拆股日历(一次拉全市场,过滤我们的票),做成全局缓存
_NASDAQ_SPLITS = None

def prefetch_nasdaq_splits():
    global _NASDAQ_SPLITS
    if _NASDAQ_SPLITS is not None:
        return _NASDAQ_SPLITS
    _NASDAQ_SPLITS = {"status": "unavailable", "by_ticker": {}}
    try:
        r = _get_retry("https://api.nasdaq.com/api/calendar/splits",
                       headers=_HDR_BROWSER, timeout=20)
        if r.status_code == 200:
            rows = ((r.json().get("data") or {}).get("rows")) or []
            bt = {}
            for x in rows:
                sym = (x.get("symbol") or "").upper()
                ratio = (x.get("ratio") or "").replace(" ", "")
                bt.setdefault(sym, []).append(
                    Event(sym, "split", "Nasdaq",
                          ex_date=_norm_date_us(x.get("executionDate")),
                          ratio=ratio, raw=x))
            _NASDAQ_SPLITS = {"status": "ok", "by_ticker": bt}
    except Exception:
        pass
    return _NASDAQ_SPLITS


def fetch_nasdaq_splits(ticker: str) -> SourceResult:
    cal = prefetch_nasdaq_splits()
    if cal["status"] != "ok":
        return SourceResult("Nasdaq", ticker, "unavailable", detail="拆股日历不可用")
    return SourceResult("Nasdaq", ticker, "ok", cal["by_ticker"].get(ticker.upper(), []))


# ---------------- 6) Tiingo(分红/拆股,免费 key)----------------
def fetch_tiingo(ticker: str, token: str) -> List[SourceResult]:
    if not token:
        return [SourceResult("Tiingo", ticker, "unavailable", detail="未配置 token")]
    start = (dt.date.today() - dt.timedelta(days=C.LOOKBACK_DAYS + 800)).isoformat()
    url = f"https://api.tiingo.com/tiingo/daily/{ticker}/prices"
    try:
        r = requests.get(url, params={"startDate": start, "token": token, "format": "json"},
                         timeout=25)
        if r.status_code != 200:
            return [SourceResult("Tiingo", ticker, "unavailable", detail=f"HTTP {r.status_code}")]
        data = r.json()
        divs, splits = [], []
        for x in data:
            d = _norm_date(x.get("date"))
            if x.get("divCash"):
                amt = _f(x.get("divCash"))
                if amt and amt > 0:
                    divs.append(Event(ticker, "dividend", "Tiingo", ex_date=d, amount=amt, raw=x))
            sf = x.get("splitFactor")
            if sf and float(sf) != 1.0:
                splits.append(Event(ticker, "split", "Tiingo", ex_date=d,
                                    ratio=_ratio_from_float(sf), raw=x))
        return [SourceResult("Tiingo", ticker, "ok", divs),
                SourceResult("Tiingo", ticker, "ok", splits)]
    except Exception as e:
        return [SourceResult("Tiingo", ticker, "unavailable", detail=f"{e}")]


# ---------------- 7) Alpaca(并购/分拆/退市等结构化,批量,免费 key)----------------
_ALPACA = None

def prefetch_alpaca(tickers, key_id, secret):
    """一次批量拉所有票的公司行动。返回 {ticker: [Event,...]} + 全局 status。"""
    global _ALPACA
    if _ALPACA is not None:
        return _ALPACA
    _ALPACA = {"status": "unavailable", "by_ticker": {}, "detail": ""}
    if not (key_id and secret):
        _ALPACA["detail"] = "未配置 Alpaca key"
        return _ALPACA
    start = (dt.date.today() - dt.timedelta(days=C.LOOKBACK_DAYS)).isoformat()
    end = (dt.date.today() + dt.timedelta(days=C.LOOKAHEAD_DAYS)).isoformat()
    # 不传 types:返回全部类型(cash_dividends/forward_splits/reverse_splits/
    # unit_splits/spin_offs/*_mergers/name_changes/... 复数键)
    headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret,
               "accept": "application/json"}
    bt = {}
    page_token = None
    try:
        for _ in range(10):  # 翻页保护
            params = {"symbols": ",".join(tickers),
                      "start": start, "end": end, "limit": 1000}
            if page_token:
                params["page_token"] = page_token
            r = requests.get("https://data.alpaca.markets/v1/corporate-actions",
                             headers=headers, params=params, timeout=30)
            if r.status_code != 200:
                _ALPACA["detail"] = f"HTTP {r.status_code}: {r.text[:80]}"
                return _ALPACA
            j = r.json()
            ca = j.get("corporate_actions", {}) or {}
            for kind, items in ca.items():
                for x in items:
                    sym = (x.get("symbol") or x.get("target_symbol") or "").upper()
                    if not sym:
                        continue
                    if "dividend" in kind:
                        bt.setdefault(sym, []).append(Event(
                            sym, "dividend", "Alpaca",
                            ex_date=_norm_date(x.get("ex_date")),
                            record_date=_norm_date(x.get("record_date")),
                            pay_date=_norm_date(x.get("payable_date")),
                            declaration_date=_norm_date(x.get("declaration_date")),
                            amount=_f(x.get("rate")), raw=x))
                    elif "split" in kind:
                        nd = x.get("new_rate"); od = x.get("old_rate")
                        ratio = f"{int(float(nd))}:{int(float(od))}" if nd and od else None
                        bt.setdefault(sym, []).append(Event(
                            sym, "split", "Alpaca",
                            ex_date=_norm_date(x.get("ex_date") or x.get("process_date")),
                            ratio=ratio, raw=x))
                    else:  # merger / spinoff / name_change / symbol_change
                        bt.setdefault(sym, []).append(Event(
                            sym, "filing", "Alpaca",
                            ex_date=_norm_date(x.get("process_date") or x.get("effective_date")),
                            note=f"{kind} · {x.get('target_symbol','') or x.get('new_symbol','')}".strip(" ·"),
                            raw=x))
            page_token = j.get("next_page_token")
            if not page_token:
                break
        _ALPACA = {"status": "ok", "by_ticker": bt, "detail": ""}
    except Exception as e:
        _ALPACA["detail"] = str(e)
    return _ALPACA


def fetch_alpaca(ticker: str) -> List[SourceResult]:
    if _ALPACA is None or _ALPACA["status"] != "ok":
        detail = (_ALPACA or {}).get("detail", "未初始化")
        return [SourceResult("Alpaca", ticker, "unavailable", detail=detail)]
    evs = _ALPACA["by_ticker"].get(ticker.upper(), [])
    # 按类型分组返回(便于核对引擎按 etype 统计覆盖)
    out = []
    for et in ("dividend", "split", "filing"):
        out.append(SourceResult("Alpaca", ticker, "ok", [e for e in evs if e.etype == et]))
    return out


# ---------------- 汇总单只票的所有源 ----------------
def fetch_all_for_ticker(ticker: str, keys: dict, av_enabled: bool = True) -> List[SourceResult]:
    results = []
    results += fetch_yfinance(ticker)
    results += fetch_fmp(ticker, keys["FMP"])
    if av_enabled and keys.get("ALPHAVANTAGE"):
        results += fetch_alphavantage(ticker, keys["ALPHAVANTAGE"])
    results.append(fetch_sec(ticker, C.LOOKBACK_DAYS))
    # 加强源
    results.append(fetch_nasdaq_dividends(ticker))
    results.append(fetch_nasdaq_splits(ticker))
    if keys.get("TIINGO"):
        results += fetch_tiingo(ticker, keys["TIINGO"])
    results += fetch_alpaca(ticker)   # 需先调用 prefetch_alpaca
    return results
