# -*- coding: utf-8 -*-
"""合约公司行动操作门槛。

这个模块只做一次判定，网页、Lark 与交互 Bot 均消费下发的 ``contract_action``，
避免不同展示面各自解释 3% 规则。
"""
import datetime as dt
import math
import re
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction

import config as C


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _fmt_number(value):
    if value is None:
        return "—"
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _positive_decimal(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


def _positive_fraction(value):
    """把供应商 float 还原成常见有理比例，避免 3/97 的浮点尾差越过门槛。"""
    try:
        number = Fraction(str(value)).limit_denominator(1_000_000_000)
    except (ValueError, ZeroDivisionError, TypeError):
        return None
    return number if number > 0 else None


def _above_threshold(raw_impact):
    threshold = C.CONTRACT_PRICE_IMPACT_THRESHOLD_PCT
    if isinstance(raw_impact, Fraction):
        return raw_impact > Fraction(str(threshold))
    return raw_impact > Decimal(str(threshold))


def _impact_value(raw_impact):
    """JSON 只存数值；比较保留 Decimal/Fraction 精度，展示最多保留 9 位。"""
    return round(float(raw_impact), 9)


def _impact_text(raw_impact):
    value = float(raw_impact)
    # 临界附近多给小数，避免 UI 显示“3.00%”却又说严格超过 3%。
    if abs(value - C.CONTRACT_PRICE_IMPACT_THRESHOLD_PCT) < 0.01:
        return f"{value:.9f}".rstrip("0").rstrip(".")
    return f"{value:.2f}"


def value_display(etype, *, amount=None, ratio=None, subtype="", amount_currency="",
                  amount_unit=""):
    """统一的用户可见数值，避免把送股比例或 ADR 本币金额误写成美元。"""
    if etype == "split":
        return str(ratio or "")
    if etype != "dividend":
        return ""
    number = _number(amount)
    if number is None:
        return ""
    if subtype == "stock_dividend":
        return f"送股 {_fmt_number(number)} 股/股（{_fmt_number(number * 100)}%）"

    currency = str(amount_currency or "").upper()
    if currency == "USD":
        text = f"${_fmt_number(number)}"
    elif currency:
        text = f"{currency} {_fmt_number(number)}"
    else:
        text = _fmt_number(number)
    unit_suffix = {
        "listed_security": "",
        "ordinary_share": "/普通股",
        "ADS": "/ADS",
        "ADR": "/ADR",
    }.get(amount_unit, f"/{amount_unit}" if amount_unit else "")
    text += unit_suffix
    if not currency or not amount_unit:
        text += "（币种/单位待核实）"
    return text


def event_label(etype, subtype=""):
    if etype == "dividend" and subtype == "stock_dividend":
        return "送股"
    return {"dividend": "分红", "split": "拆股/合股", "filing": "并购/公告"}.get(etype, etype)


def _base(status, message, **extra):
    return {
        "status": status,
        "threshold_pct": C.CONTRACT_PRICE_IMPACT_THRESHOLD_PCT,
        "message": message,
        **extra,
    }


def _price_fields(reference_price):
    snap = reference_price if isinstance(reference_price, dict) else {}
    return {
        "reference_price": _number(snap.get("value")),
        "price_as_of": snap.get("date") or "",
        "price_source": snap.get("source") or "",
        "price_basis": snap.get("basis") or "previous_session_unadjusted_close",
        "price_currency": str(snap.get("currency") or "").upper(),
        "price_unit": snap.get("unit") or "",
    }


def _price_date_problem(price_date, today):
    if not price_date or today is None:
        return "缺少参考价日期"
    try:
        as_of = dt.date.fromisoformat(price_date)
        day = today if isinstance(today, dt.date) else dt.date.fromisoformat(str(today))
    except (TypeError, ValueError):
        return "参考价日期格式无效"
    if as_of >= day:
        return "参考价不是前一完整交易日"
    if (day - as_of).days > C.CONTRACT_REFERENCE_PRICE_MAX_AGE_DAYS:
        return f"参考价日期 {price_date} 已超过 {C.CONTRACT_REFERENCE_PRICE_MAX_AGE_DAYS} 天"
    return ""


def _ratio_parts(ratio):
    if ratio is None:
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?::|-|for)\s*(\d+(?:\.\d+)?)\s*", str(ratio), re.I)
    if not match:
        return None
    new, old = Fraction(match.group(1)), Fraction(match.group(2))
    return (new, old) if new > 0 and old > 0 else None


def action_subtype(by_source, etype):
    """从各源保留的 subtype 中识别现金分红/送股；拆合股沿用 split。"""
    if etype == "split":
        return "split"
    values = {
        str(fields.get("subtype") or "").lower()
        for fields in (by_source or {}).values()
        if fields.get("subtype")
    }
    stock = any("stock" in value or "bonus" in value for value in values)
    cash = any("cash" in value for value in values)
    if stock and cash:
        return "mixed_dividend"
    if stock:
        return "stock_dividend"
    return "cash_dividend" if etype == "dividend" else etype


def evaluate(ticker, etype, *, amount=None, ratio=None, subtype="", reference_price=None,
             amount_currency="", amount_unit="", value_verified=False, forecast=False,
             disputed=False, filing_relevant=None, today=None):
    """返回 required / not_required / review / not_applicable 四态判定。"""
    if ticker not in C.CONTRACT_TICKERS:
        return _base("not_applicable", "")

    price = _price_fields(reference_price)
    # 原始供应商价格只留在内部 cache，不下发到公开 Pages；展示只需要结论、影响率和日期。
    common = {
        "impact_pct": None,
        "subtype": subtype or etype,
        "price_as_of": price["price_as_of"],
        "price_basis": price["price_basis"],
    }

    if forecast:
        return _base(
            "review",
            "合约：待核实｜公司行动本身仍是预测，未证实前不得执行",
            **common,
        )
    if disputed or not value_verified:
        return _base(
            "review",
            "合约：待核实｜金额或比例尚未通过门禁，暂不能判定是否操作",
            **common,
        )

    if etype == "dividend" and subtype == "mixed_dividend":
        return _base("review", "合约：待核实｜数据源对现金分红/送股类型判断不一致", **common)

    if etype == "dividend" and subtype == "stock_dividend":
        rate = _positive_fraction(amount)
        if rate is None or amount_unit != "additional_share_per_share":
            return _base("review", "合约：待核实｜缺少可验证的每股送股比例", **common)
        raw_impact = rate / (1 + rate) * 100
        impact = _impact_value(raw_impact)
        common["impact_pct"] = impact
        detail = f"送股对理论除权价影响约 {_impact_text(raw_impact)}%"
        if _above_threshold(raw_impact):
            return _base("required", f"合约：需操作｜{detail}，严格超过 3%", **common)
        return _base("not_required", f"合约：本次无需操作｜{detail}，未超过 3%", **common)

    if etype == "dividend":
        cash = _number(amount)
        ref = price["reference_price"]
        if cash is None:
            return _base("review", "合约：待核实｜缺少有效的每股现金分红金额", **common)
        if ref is None:
            return _base(
                "review",
                "合约：待核实｜缺少最近有效参考价，暂不能计算现金分红影响",
                **common,
            )
        if (not amount_currency or not amount_unit or not price["price_currency"] or
                not price["price_unit"]):
            return _base(
                "review",
                "合约：待核实｜分红金额或参考价缺少币种/证券单位，暂不能安全计算 3%",
                **common,
            )
        if (str(amount_currency).upper() != price["price_currency"] or
                amount_unit != price["price_unit"]):
            return _base(
                "review",
                "合约：待核实｜分红金额与参考价的币种或证券单位不一致（ADR/ADS 需特别核对）",
                **common,
            )
        date_problem = _price_date_problem(price["price_as_of"], today)
        if date_problem:
            return _base(
                "review",
                f"合约：待核实｜{date_problem}",
                **common,
            )
        cash_decimal = _positive_decimal(amount)
        ref_decimal = _positive_decimal((reference_price or {}).get("value"))
        if cash_decimal is None or ref_decimal is None:
            return _base("review", "合约：待核实｜现金分红金额或参考价格式无效", **common)
        with localcontext() as ctx:
            ctx.prec = 40
            raw_impact = cash_decimal / ref_decimal * Decimal(100)
        impact = _impact_value(raw_impact)
        detail = (f"现金分红影响约 {_impact_text(raw_impact)}%（按 {price['price_as_of']} "
                  "前一完整交易日未调整收盘价估算）")
        common["impact_pct"] = impact
        if _above_threshold(raw_impact):
            return _base("required", f"合约：需操作｜{detail}，严格超过 3%", **common)
        return _base("not_required", f"合约：本次无需操作｜{detail}，未超过 3%", **common)

    if etype == "split":
        parts = _ratio_parts(ratio)
        if not parts:
            return _base("review", "合约：待核实｜缺少有效的送股/拆股/合股比例", **common)
        new, old = parts
        raw_impact = abs(old / new - 1) * 100
        impact = _impact_value(raw_impact)
        common["impact_pct"] = impact
        detail = f"拆股/合股 {ratio} 对理论除权价影响约 {_impact_text(raw_impact)}%"
        if _above_threshold(raw_impact):
            return _base("required", f"合约：需操作｜{detail}，严格超过 3%", **common)
        return _base("not_required", f"合约：本次无需操作｜{detail}，未超过 3%", **common)

    if etype == "filing" and filing_relevant is False:
        return _base("not_required", "合约：本次无需操作｜普通备案不属于合约价格调整事项", **common)
    return _base("review", "合约：待核实｜结构性公司行动需确认条款及价格影响是否超过 3%", **common)


def follow_up_mode(ticker, decision):
    """execution=执行催办；verification=只核验；none=不进入周期提醒。"""
    if ticker in C.SPOT_TICKERS:
        return "execution"
    status = (decision or {}).get("status")
    if status == "required":
        return "execution"
    if status == "review":
        return "verification"
    return "none"


def reminder_state_suffix(ticker, decision, mode):
    """合约门槛使用独立去重轨，价格跨过 3% 时不会被旧提醒吞掉。"""
    if mode == "verification" and ticker in C.CONTRACT_TICKERS:
        return "contract-review"
    if (decision or {}).get("status") == "required" and ticker in C.CONTRACT_TICKERS:
        return "contract-action"
    return ""
