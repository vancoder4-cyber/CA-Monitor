# -*- coding: utf-8 -*-
"""五源数据抓取器。

每个抓取器返回 SourceResult,关键设计:
- status="ok"      源正常应答(events 可能为空 = "源说这只票没有此类事件")
- status="unavailable" 源被限流 / 付费墙 / 报错(= "没查到" != "源说没有")
这区分让交叉核对不会把"源不可用"误判成"空缺"。
"""
import os
import re
import json
import time
import datetime as dt
import threading
import math
from fractions import Fraction
from dataclasses import dataclass, field
from typing import List, Optional

import requests
import config as C
from business_time import today as business_today


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
    subtype: str = ""                   # cash_dividend | stock_dividend | split ...
    amount_currency: str = ""           # USD 等；现金分红必须与参考价同币种
    amount_unit: str = ""               # listed_security | additional_share_per_share
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


# 合约 3% 门槛使用的行情快照。每只标的抓取过程中记录前一完整交易日的
# 未调整收盘价；同日优先 Tiingo，其次 yfinance。快照随缓存落盘，
# build/各展示面不再自行联网取价。
_REFERENCE_PRICES = {}
_REFERENCE_PRICE_LOCK = threading.Lock()
_REFERENCE_PRICE_PRIORITY = {"Tiingo": 0, "yfinance": 1}


def clear_reference_price(ticker):
    with _REFERENCE_PRICE_LOCK:
        _REFERENCE_PRICES.pop(ticker.upper(), None)


def replace_reference_prices(prices):
    """以磁盘中的 last-known-good 快照初始化本轮；源短暂失败时不丢失有效价。"""
    cleaned = {}
    for ticker, snap in (prices or {}).items():
        if not isinstance(snap, dict):
            continue
        value = _f(snap.get("value"))
        date = _norm_date(snap.get("date"))
        currency = str(snap.get("currency") or "").upper()
        unit = str(snap.get("unit") or "")
        if (value is None or not math.isfinite(value) or value <= 0 or not date
                or not currency or not unit):
            continue
        cleaned[str(ticker).upper()] = {
            "value": value,
            "date": date,
            "source": snap.get("source") or "",
            "basis": snap.get("basis") or "previous_session_unadjusted_close",
            "currency": currency,
            "unit": unit,
        }
    with _REFERENCE_PRICE_LOCK:
        _REFERENCE_PRICES.clear()
        _REFERENCE_PRICES.update(cleaned)


def all_reference_prices():
    with _REFERENCE_PRICE_LOCK:
        return {ticker: dict(snap) for ticker, snap in _REFERENCE_PRICES.items()}


def record_reference_price(ticker, value, date, source):
    value = _f(value)
    date = _norm_date(date)
    if value is None or not math.isfinite(value) or value <= 0 or not date:
        return
    candidate = {
        "value": value,
        "date": date,
        "source": source,
        "basis": "previous_session_unadjusted_close",
        "currency": "USD",
        "unit": "listed_security",
    }
    key = ticker.upper()
    with _REFERENCE_PRICE_LOCK:
        current = _REFERENCE_PRICES.get(key)
        if (not current or date > current.get("date", "") or
                (date == current.get("date") and
                 _REFERENCE_PRICE_PRIORITY.get(source, 99) <
                 _REFERENCE_PRICE_PRIORITY.get(current.get("source"), 99))):
            _REFERENCE_PRICES[key] = candidate


def reference_price(ticker):
    with _REFERENCE_PRICE_LOCK:
        current = _REFERENCE_PRICES.get(ticker.upper())
        return dict(current) if current else None


def _capture_yfinance_reference_price(ticker, yf_ticker):
    """记录前一完整交易日收盘价；当天价格即使收盘后也不作为本轮分母。"""
    try:
        # 1mo 兼容 requirements 允许的 yfinance 0.2.40；旧版不接受自定义 10d。
        history = yf_ticker.history(period="1mo", auto_adjust=False, actions=False)
        rows = []
        for stamp, close in history["Close"].items():
            day = stamp.date() if hasattr(stamp, "date") else dt.date.fromisoformat(str(stamp)[:10])
            value = _f(close)
            if (day < business_today() and value is not None and
                    math.isfinite(value) and value > 0):
                rows.append((day.isoformat(), value))
        if rows:
            day, close = max(rows, key=lambda item: item[0])
            record_reference_price(ticker, close, day, "yfinance")
    except Exception:
        # 行情缺失只会让合约判定进入 review，不应拖垮公司行动抓取。
        return


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

    _capture_yfinance_reference_price(ticker, t)

    # 分红
    dev = []
    try:
        for d, amt in divs.items():
            dev.append(Event(ticker, "dividend", "yfinance",
                             ex_date=_norm_date(d), amount=_f(amt), subtype="cash_dividend",
                             amount_currency="USD", amount_unit="listed_security"))
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
        value = float(f)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    # 不能 round 成整数：1.05 是 21:20（约 4.76% 价格影响），不是 1:1。
    frac = Fraction(str(value)).limit_denominator(1_000_000)
    if frac <= 0:
        return None
    return f"{frac.numerator}:{frac.denominator}"


def _ratio_from_pair(new, old):
    """把供应商的 new_rate / old_rate 保真归一化成整数比。"""
    try:
        ratio = Fraction(str(new)) / Fraction(str(old))
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if ratio <= 0:
        return None
    ratio = ratio.limit_denominator(1_000_000)
    if ratio <= 0:
        return None
    return f"{ratio.numerator}:{ratio.denominator}"


def normalize_ratio(value):
    """确认入口既接受完整 new:old，也兼容旧式单一 split factor。"""
    if value is None:
        return None
    text = str(value).strip().replace("：", ":")
    match = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*(?::|-|for)\s*(\d+(?:\.\d+)?)",
        text,
        re.I,
    )
    if match:
        return _ratio_from_pair(match.group(1), match.group(2))
    return _ratio_from_float(text)


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
                             amount=_f(x.get("dividend")), subtype="cash_dividend",
                             amount_currency="USD", amount_unit="listed_security", raw=x)
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
                    ratio = _ratio_from_pair(num, den) if num and den else None
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
                         amount=_f(x.get("amount")), subtype="cash_dividend",
                         amount_currency="USD", amount_unit="listed_security", raw=x)
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

# Foreign private issuers file Form 6-K instead of 8-K.  The submissions feed
# does not expose 8-K-style Item codes for 6-K, and most 6-Ks are ordinary
# earnings / monthly-sales / governance disclosures.  Only strong metadata
# hints are therefore promoted to *review* (relevant=None); a human or a
# structured source still has to confirm the actual corporate action.
_SEC_6K_CA_PATTERNS = (
    (r"\b(?:cash|quarterly|interim|special|final|annual)?\s*dividends?\b", "分红"),
    (r"\b(?:cash|stock|share)\s+distributions?\b", "分派"),
    (r"\b(?:stock|share)\s+splits?\b|\bsubdivision\s+of\s+shares?\b", "拆股"),
    (r"\breverse\s+(?:stock|share)?\s*splits?\b|\b(?:share|stock)\s+consolidations?\b", "合股"),
    (r"\bspin[ -]?offs?\b|\bspin[ -]?outs?\b", "分拆"),
    (r"\bmergers?\b", "并购"),
    (r"\bacquisitions?\b", "收购"),
    (r"\btender\s+offers?\b", "要约收购"),
    (r"\bschemes?\s+of\s+arrangement\b", "安排计划"),
    (r"\bdelist(?:ing|ed)?\b", "退市"),
    (r"\b(?:ticker|symbol)\s+changes?\b", "代码变更"),
    (r"\bname\s+changes?\b", "名称变更"),
    (r"\brights?\s+offerings?\b|\b(?:shareholders?|shares?|equity)\s+rights?\s+issues?\b|"
     r"\brights?\s+issues?\s+(?:of|to)\s+(?:shares?|shareholders?)\b", "供股"),
    (r"\b(?:share|stock|equity)\s+redemptions?\b|\bredemption\s+of\s+(?:shares?|stock|equity)\b", "股份赎回"),
)

# These phrases routinely occur in 6-K metadata but are not equity corporate
# actions.  Remove them before matching so a bond redemption or financial
# statement heading cannot create a false company-action review.
_SEC_6K_NON_CA_PATTERNS = (
    r"\bdistribution\s+agreements?\b",
    r"\bconsolidat(?:ed|ion)\s+(?:of\s+)?financial\s+(?:results?|statements?|information)\b",
    r"\bredemption\s+of\s+(?:(?:senior|subordinated|convertible)\s+)?(?:notes?|bonds?|debentures?)\b",
    r"\bhuman\s+rights?\s+issues?\b",
)


def _sec_filing_note_relevance(form, items_str="", primary_document="",
                               primary_description=""):
    """Return ``(note, relevant)`` for a SEC filing.

    ``True`` means the form metadata alone proves a structural action;
    ``False`` means it is kept only in the raw SEC audit table; ``None`` means
    a conservative 6-K metadata hint requires verification.  This tri-state
    contract is consumed centrally by ``run.py`` and every renderer.
    """
    if form in ("8-K", "8-K/A"):
        descs, relevant = C.describe_8k(items_str)
        return f"{form} · " + ("、".join(descs) if descs else "重大事件"), relevant

    if form in ("6-K", "6-K/A"):
        # Filenames/descriptions vary (spaces, hyphens, underscores), but broad
        # substring matching is unsafe: "Distribution Agreement",
        # "Consolidation of Financial Results" and senior-note redemptions are
        # common non-CA filings.  Match explicit equity-action phrases only.
        haystack = re.sub(
            r"[_\-]+", " ",
            f"{primary_document or ''} {primary_description or ''}".lower(),
        )
        haystack = re.sub(r"\.[a-z0-9]{1,5}\b", " ", haystack)
        haystack = re.sub(r"\s+", " ", haystack)
        for pattern in _SEC_6K_NON_CA_PATTERNS:
            haystack = re.sub(pattern, " ", haystack)
        hits = []
        for pattern, label in _SEC_6K_CA_PATTERNS:
            if re.search(pattern, haystack) and label not in hits:
                hits.append(label)
        # Some issuers concatenate words in the primary-document filename.
        # Keep a deliberately short allowlist for known unambiguous compounds;
        # do not reintroduce generic distribution/consolidation/redemption.
        compact = re.sub(r"[^a-z0-9]+", "", haystack)
        compact = re.sub(r"humanrightsissues?", "", compact)
        for token, label in (
            ("dividendadjustment", "分红"),
            ("stocksplit", "拆股"),
            ("sharesplit", "拆股"),
            ("reversestocksplit", "合股"),
            ("reversesharesplit", "合股"),
            ("tenderoffer", "要约收购"),
            ("schemeofarrangement", "安排计划"),
            ("symbolchange", "代码变更"),
            ("namechange", "名称变更"),
            ("rightsoffering", "供股"),
            ("rightsissue", "供股"),
        ):
            if token in compact and label not in hits:
                hits.append(label)
        if hits:
            return f"{form} · 疑似公司行动（{'、'.join(hits)}，待核实）", None
        return f"{form} · 外国发行人普通备案", False

    note = f"{form} · {C.SEC_FORMS_OF_INTEREST[form]}"
    relevant_forms = {
        "25", "25-NSE", "425", "S-4", "DEFM14A", "8-K12B", "15-12B",
        "SC TO-I", "SC 14D9",
    }
    return note, form in relevant_forms

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
        descriptions = recent.get("primaryDocDescription", [])
        items_all = recent.get("items", [])
        accepted_all = recent.get("acceptanceDateTime", [])
        cutoff = (business_today() - dt.timedelta(days=lookback_days)).isoformat()
        evs = []
        for i, form in enumerate(forms):
            if form not in C.SEC_FORMS_OF_INTEREST:
                continue
            fdate = dates[i] if i < len(dates) else None
            if fdate and fdate < cutoff:
                continue
            accn = accns[i].replace("-", "") if i < len(accns) else ""
            doc = docs[i] if i < len(docs) else ""
            description = descriptions[i] if i < len(descriptions) else ""
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn}/{doc}" if accn else ""
            items_str = items_all[i] if i < len(items_all) else ""
            accepted = (accepted_all[i] if i < len(accepted_all) else "") or ""
            accepted = accepted.replace("T", " ")[:16]   # 'YYYY-MM-DD HH:MM'
            note, relevant = _sec_filing_note_relevance(
                form, items_str, doc, description,
            )
            evs.append(Event(ticker, "filing", "SEC", ex_date=fdate, note=note,
                             raw={"form": form, "url": url, "items": items_str,
                                  "relevant": relevant, "accepted": accepted,
                                  "primary_description": description}))
        return SourceResult("SEC", ticker, "ok", evs)
    except Exception as e:
        return SourceResult("SEC", ticker, "unavailable", detail=f"{e}")


# ---------------- 5) Nasdaq(免 key:按票分红 + 市场拆股日历)----------------
def nasdaq_dividend_url(ticker: str) -> str:
    asset_class = "etf" if C.asset_type(ticker) == "etf" else "stocks"
    return f"https://api.nasdaq.com/api/quote/{ticker}/dividends?assetclass={asset_class}"


def fetch_nasdaq_dividends(ticker: str) -> SourceResult:
    url = nasdaq_dividend_url(ticker)
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
                             amount=_money(x.get("amount")), subtype="cash_dividend",
                             amount_currency="USD", amount_unit="listed_security", raw=x))
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
    start = (business_today() - dt.timedelta(days=C.LOOKBACK_DAYS + 800)).isoformat()
    url = f"https://api.tiingo.com/tiingo/daily/{ticker}/prices"
    try:
        r = requests.get(url, params={"startDate": start, "token": token, "format": "json"},
                         timeout=25)
        if r.status_code != 200:
            return [SourceResult("Tiingo", ticker, "unavailable", detail=f"HTTP {r.status_code}")]
        data = r.json()
        divs, splits = [], []
        completed_prices = []
        for x in data:
            d = _norm_date(x.get("date"))
            close = _f(x.get("close"))
            if (d and d < business_today().isoformat() and close is not None and
                    math.isfinite(close) and close > 0):
                completed_prices.append((d, close))
            if x.get("divCash"):
                amt = _f(x.get("divCash"))
                if amt and amt > 0:
                    divs.append(Event(ticker, "dividend", "Tiingo", ex_date=d, amount=amt,
                                      subtype="cash_dividend", amount_currency="USD",
                                      amount_unit="listed_security", raw=x))
            sf = x.get("splitFactor")
            if sf and float(sf) != 1.0:
                splits.append(Event(ticker, "split", "Tiingo", ex_date=d,
                                    ratio=_ratio_from_float(sf), raw=x))
        if completed_prices:
            price_day, close = max(completed_prices, key=lambda item: item[0])
            record_reference_price(ticker, close, price_day, "Tiingo")
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
    today = business_today()
    start = (today - dt.timedelta(days=C.LOOKBACK_DAYS)).isoformat()
    end = (today + dt.timedelta(days=C.LOOKAHEAD_DAYS)).isoformat()
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
                        subtype = "stock_dividend" if "stock" in kind else "cash_dividend"
                        amount_unit = ("additional_share_per_share" if subtype == "stock_dividend"
                                       else "listed_security")
                        bt.setdefault(sym, []).append(Event(
                            sym, "dividend", "Alpaca",
                            ex_date=_norm_date(x.get("ex_date")),
                            record_date=_norm_date(x.get("record_date")),
                            pay_date=_norm_date(x.get("payable_date")),
                            declaration_date=_norm_date(x.get("declaration_date")),
                            amount=_f(x.get("rate")), subtype=subtype,
                            amount_currency=(x.get("currency") or "USD") if subtype == "cash_dividend" else "",
                            amount_unit=amount_unit, raw=x))
                    elif "split" in kind:
                        nd = x.get("new_rate"); od = x.get("old_rate")
                        ratio = _ratio_from_pair(nd, od) if nd and od else None
                        bt.setdefault(sym, []).append(Event(
                            sym, "split", "Alpaca",
                            ex_date=_norm_date(x.get("ex_date") or x.get("process_date")),
                            ratio=ratio, raw=x))
                    else:  # merger / spinoff / name_change / symbol_change / unknown
                        known_structural = any(token in kind for token in (
                            "merger", "spin_off", "spinoff", "name_change", "symbol_change",
                            "redemption", "rights", "worthless", "liquidation",
                        ))
                        bt.setdefault(sym, []).append(Event(
                            sym, "filing", "Alpaca",
                            ex_date=_norm_date(x.get("process_date") or x.get("effective_date")),
                            note=f"{kind} · {x.get('target_symbol','') or x.get('new_symbol','')}".strip(" ·"),
                            raw={**x, "relevant": True if known_structural else None}))
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


# ---------------- 8) FINX / TRKD-HS(静态数据 API,JWT,凭证缺失即跳过)----------------
# 接口仍在调整中(供方告知约 2 周、demo 阶段),字段以 ReDoc 文档为准、做防御式解析。
# 认证:POST {base}/auth/token {username,password} -> {token};其余请求带 header x-auth-token: <token>。
_FINX_TOKEN = None          # 进程级缓存,避免每只票都登一次
_FINX_AUTH_FAILED = False    # 登录失败就别再重试(凭证错/接口未就绪)


def _finx_token(user, pwd, base):
    global _FINX_TOKEN, _FINX_AUTH_FAILED
    if _FINX_TOKEN:
        return _FINX_TOKEN
    if _FINX_AUTH_FAILED:
        return None
    try:
        r = requests.post(f"{base}/auth/token",
                          json={"username": user, "password": pwd}, timeout=25)
        if r.status_code == 200:
            tok = (r.json() or {}).get("token")
            if tok:
                _FINX_TOKEN = tok
                return tok
        _FINX_AUTH_FAILED = True
    except Exception:
        _FINX_AUTH_FAILED = True
    return None


def _finx_get(path, token, base, params=None):
    r = requests.get(f"{base}{path}", headers={"x-auth-token": token},
                     params=params, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    j = r.json()
    return j if isinstance(j, list) else (j.get("data") if isinstance(j, dict) else []) or []


def _pick(x, *names):
    """从多个候选字段名里取第一个非空值(接口字段可能微调)。"""
    for n in names:
        v = x.get(n)
        if v not in (None, "", 0, "0"):
            return v
    # 再容忍 0 值(金额可能真为 0,但日期不会)
    for n in names:
        if n in x and x.get(n) not in (None, ""):
            return x.get(n)
    return None


def _finx_ratio(r):
    """FINX 拆股比例 '1-250' → '1:250'(与其它源统一,避免格式造成假冲突)。"""
    if r is None:
        return None
    s = str(r).strip()
    m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", s)
    return f"{m.group(1)}:{m.group(2)}" if m else s


def fetch_finx(ticker: str, user: str, pwd: str, base: str = "") -> List[SourceResult]:
    base = (base or C.FINX_BASE_DEFAULT).rstrip("/")
    if not (user and pwd):
        return [SourceResult("FINX", ticker, "unavailable", detail="未配置 FINX 凭证")]
    ric = C.finx_ric(ticker)
    if not ric:
        return [SourceResult("FINX", ticker, "unavailable", detail="无 RIC(非个股/ETF)")]
    token = _finx_token(user, pwd, base)
    if not token:
        return [SourceResult("FINX", ticker, "unavailable", detail="认证失败/接口未就绪")]

    out = []
    # 分红:事件接口 DIVIDEND(含宣告日);拿不到再退派发历史
    try:
        rows = _finx_get(f"/event/{ric}/DIVIDEND", token, base)
        if not rows:
            rows = _finx_get(f"/financial/dividend/payout/history/{ric}", token, base)
        evs = []
        for x in rows:
            if not isinstance(x, dict):
                continue
            evs.append(Event(
                ticker, "dividend", "FINX",
                # 实测字段名(事件接口 / 派发历史接口两套都兼容)
                ex_date=_norm_date(_pick(x, "dividendExDate", "divExDate", "exDate")),
                record_date=_norm_date(_pick(x, "dividendRecordDate", "divRecordDate", "recordDate")),
                pay_date=_norm_date(_pick(x, "dividendPaymentDate", "divPayDate", "payDate")),
                declaration_date=_norm_date(_pick(x, "annoucementDate", "announcementDate", "declarationDate")),
                amount=_f(_pick(x, "dividendAmount", "divRate", "amount")),
                subtype="cash_dividend",
                amount_currency="USD",
                amount_unit="listed_security",
                raw=x))
        out.append(SourceResult("FINX", ticker, "ok", evs))
    except Exception as e:
        out.append(SourceResult("FINX", ticker, "unavailable", detail=f"div:{e}"))

    # 拆股:事件接口 STOCK_SPLIT
    try:
        rows = _finx_get(f"/event/{ric}/STOCK_SPLIT", token, base)
        evs = []
        for x in rows:
            if not isinstance(x, dict):
                continue
            ratio = _finx_ratio(_pick(x, "splitRatio", "ratio"))
            evs.append(Event(
                ticker, "split", "FINX",
                ex_date=_norm_date(_pick(x, "splitExDate", "exDate")),
                record_date=_norm_date(_pick(x, "splitRecordDate")),
                pay_date=_norm_date(_pick(x, "splitPaymentDate", "payDate")),
                declaration_date=_norm_date(_pick(x, "splitAnnouncement", "splitAnnoucement", "annoucementDate", "announcementDate")),
                ratio=ratio,
                raw=x))
        out.append(SourceResult("FINX", ticker, "ok", evs))
    except Exception as e:
        out.append(SourceResult("FINX", ticker, "unavailable", detail=f"split:{e}"))

    # 并购 / 其它公司行动:作为 filing 信号
    for et, label in (("MA", "并购"), ("OTHER_CORPORATE_ACTION", "其它公司行动")):
        try:
            rows = _finx_get(f"/event/{ric}/{et}", token, base)
            evs = []
            for x in rows:
                if not isinstance(x, dict):
                    continue
                desc = _pick(x, "enDivTypeMarkerDesc", "eventType", "description") or label
                evs.append(Event(
                    ticker, "filing", "FINX",
                    ex_date=_norm_date(_pick(x, "startDate", "annoucementDate", "announcementDate", "lastUpdate")),
                    note=f"FINX · {desc}",
                    raw={**x, "relevant": True if et == "MA" else None}))
            out.append(SourceResult("FINX", ticker, "ok", evs))
        except Exception as e:
            out.append(SourceResult("FINX", ticker, "unavailable", detail=f"{et}:{e}"))
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
    # 第 8 源 FINX:仅在配置了凭证时才真正请求(否则静默跳过,不拖慢/不报错)
    if keys.get("FINX_USER") and keys.get("FINX_PASS"):
        results += fetch_finx(ticker, keys["FINX_USER"], keys["FINX_PASS"], keys.get("FINX_BASE", ""))
    return results
