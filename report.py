# -*- coding: utf-8 -*-
"""生成 HTML 面板 + 文本预警 digest + 月历视图。"""
import os
import html
import re
import calendar as _cal
import datetime as dt
from urllib.parse import urlsplit
import config as C
import reconcile as R
from business_time import today as business_today


def _business_date(meta=None):
    """优先沿用本次构建快照的美东业务日，缺失时再读取统一市场时钟。"""
    raw = (meta or {}).get("business_date")
    try:
        return dt.date.fromisoformat(raw) if raw else business_today()
    except (TypeError, ValueError):
        return business_today()


def _event_key(event):
    """跨展示面统一事件 ID；同日多份 filing 不能按 ticker/date 互相吞掉。"""
    if isinstance(event, dict):
        return event.get("event_id") or (
            event.get("ticker"), event.get("etype"), event.get("date")
        )
    return getattr(event, "event_id", None) or (
        getattr(event, "ticker", None),
        getattr(event, "etype", None),
        getattr(event, "anchor_date", None),
    )


def _is_routine_filing(event):
    if isinstance(event, dict):
        return event.get("etype") == "filing" and event.get("filing_relevant") is False
    return (getattr(event, "etype", None) == "filing" and
            getattr(event, "filing_relevant", None) is False)


def _visible_alert_items(alerts):
    """与定时 Lark 一致：同一事件在报警区只展示最高优先级的一次。"""
    claimed = set()

    def take(items):
        visible = []
        for item in items or []:
            if _is_routine_filing(item):
                continue
            key = _event_key(item)
            if key in claimed:
                continue
            claimed.add(key)
            visible.append(item)
        return visible

    actionable_rounds = [
        item for item in alerts.get("rounds", [])
        if not (isinstance(item, dict) and item.get("follow_up_mode") == "none")
    ]
    return {
        "rounds": take(actionable_rounds),
        "forecast_updates": take(alerts.get("forecast_updates", [])),
        "contract_updates": take(alerts.get("contract_updates", [])),
        "announced": take(alerts.get("announced", [])),
        "new": take(alerts.get("new", [])),
    }


def load_changelog(path=None):
    """解析 CHANGELOG.md；保留 ``items`` 平铺兼容字段及缩进列表树。"""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "CHANGELOG.md")
    if not os.path.exists(path):
        return []
    entries, cur, stack = [], None, []
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.rstrip()
            if s.startswith("## "):
                if cur:
                    entries.append(cur)
                cur = {"head": s[3:].strip(), "items": [], "tree": []}
                stack = [(-1, cur["tree"])]
                continue
            match = re.match(r"^([ \t]*)[-*+]\s+(.*)$", s)
            if match and cur is not None:
                indent = len(match.group(1).expandtabs(4))
                text = match.group(2).strip()
                node = {"text": text, "children": []}
                while len(stack) > 1 and indent <= stack[-1][0]:
                    stack.pop()
                stack[-1][1].append(node)
                stack.append((indent, node["children"]))
                # 旧调用仍可把 items 当作字符串列表消费；树只供网页保留层级。
                cur["items"].append(text)
    if cur:
        entries.append(cur)
    return entries


def _inline_markdown_html(value):
    """安全渲染更新日志需要的少量行内 Markdown。"""
    rendered = html.escape(str(value or ""), quote=True)
    placeholders = []

    def stash(markup):
        token = f"\x00CHANGELOG{len(placeholders)}\x00"
        placeholders.append((token, markup))
        return token

    # 先整体 escape，再把受控语法替换为固定标签；代码中的 Markdown 不再二次解释。
    rendered = re.sub(
        r"`([^`\n]+)`",
        lambda match: stash(f"<code>{match.group(1)}</code>"),
        rendered,
    )

    def link(match):
        label, escaped_url = match.group(1), match.group(2)
        raw_url = html.unescape(escaped_url).strip()
        try:
            parsed = urlsplit(raw_url)
        except ValueError:
            return match.group(0)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            return match.group(0)
        safe_url = html.escape(raw_url, quote=True)
        return stash(
            f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer">{label}</a>'
        )

    rendered = re.sub(r"\[([^\]\n]+)\]\(([^)\s]+)\)", link, rendered)
    rendered = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", rendered)
    for token, markup in placeholders:
        rendered = rendered.replace(token, markup)
    return rendered


def _changelog_items_html(entry):
    def render_nodes(nodes):
        if not nodes:
            return ""
        return "<ul>" + "".join(
            f"<li>{_inline_markdown_html(node.get('text', ''))}"
            f"{render_nodes(node.get('children') or [])}</li>"
            for node in nodes
        ) + "</ul>"

    tree = entry.get("tree")
    if tree is None:  # 兼容外部仍传旧的 {head, items:[str]} 结构。
        tree = [{"text": item, "children": []} for item in entry.get("items", [])]
    return render_nodes(tree)


def _changelog_entries_html(entries):
    parts = []
    for entry in entries:
        parts.append(
            "<h3 style='margin:14px 0 4px'>"
            + html.escape(str(entry.get("head", "")), quote=True)
            + "</h3>"
            + _changelog_items_html(entry)
        )
    return "".join(parts) if parts else "<p style='color:#888'>暂无</p>"

def load_refs():
    """读取当前覆盖范围内的 refs.json ir_dividend 映射。"""
    import json
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "refs.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            refs = json.load(f).get("ir_dividend", {}) or {}
        return {ticker: url for ticker, url in refs.items() if ticker in C.ALL_ASSETS}
    except Exception:
        return {}


STATUS_CN = {"confirmed": "已确认", "single": "单源待核实", "conflict": "有冲突"}
STATUS_COLOR = {"confirmed": "#1a7f37", "single": "#9a6700", "conflict": "#cf222e"}
STATUS_BG = {"confirmed": "#e8f5e9", "single": "#fff8e1", "conflict": "#ffebee"}
ETYPE_CN = {"dividend": "分红", "split": "拆股", "filing": "并购/公告"}


def _etype_label(event):
    if isinstance(event, dict):
        return event.get("event_label") or ETYPE_CN.get(event.get("etype"), event.get("etype"))
    return getattr(event, "event_label", "") or ETYPE_CN.get(event.etype, event.etype)


def _pick(g, field):
    """取值:多数票 + 源优先级(唯一真相在 reconcile.pick_value)。"""
    return R.pick_value(g.by_source, field)


def _sec_url(g):
    """filing 事件的 SEC 原文链接(没有则空)。"""
    return (g.by_source.get("SEC") or {}).get("url", "") or g.by_source.get("Alpaca", {}).get("url", "")


def _reference_html(x):
    """统一渲染 run.py 下发的分红核对链接；网页不再自行回退到 Nasdaq。"""
    if isinstance(x, dict):
        etype, ticker = x.get("etype"), x.get("ticker", "")
        links = x.get("references") or []
        primary = x.get("primary_url") or x.get("decl_url") or x.get("ir_url")
    else:
        etype, ticker = getattr(x, "etype", ""), getattr(x, "ticker", "")
        links = getattr(x, "references", []) or []
        primary = getattr(x, "primary_url", "") or getattr(x, "decl_url", "") or getattr(x, "ir_url", "")
    if etype != "dividend":
        return ""
    if not links:
        primary = primary or ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                              f"&ticker={ticker}&type=&dateb=&owner=include&count=40")
        links = [
            {"label": "SEC·公司备案", "url": primary},
            {"label": "第三方·StockAnalysis（交叉核对，可能滞后）",
             "url": C.stockanalysis_url(ticker, "dividend")},
        ]
    anchors = " · ".join(
        f"<a href='{html.escape(str(r['url']))}' target='_blank' rel='noopener'>{html.escape(str(r['label']))} ↗</a>"
        for r in links if r.get("url")
    )
    return f"<br><span style='font-size:12px'>🔗 核对: {anchors}</span>" if anchors else ""


def _fmt_event_fields(g):
    """金额/比例(详情列用):未确认冲突 → 不给确定值。"""
    if g.etype == "filing":
        return html.escape(g.note or "")
    if R.is_disputed(g):
        vals = [v.get("amount") if v.get("amount") is not None else v.get("ratio")
                for v in g.by_source.values()]
        vals = [str(v) for v in dict.fromkeys(v for v in vals if v is not None)]
        return ("<span style='color:#cf222e;font-weight:700'>⚠️各源不一致(" + html.escape(" / ".join(vals))
                + ")· 待人工确认</span>")
    display = getattr(g, "value_display", "")
    if not getattr(g, "value_verified", False) and display:
        candidate = getattr(g, "selected_amount", None)
        if candidate is None:
            candidate = getattr(g, "selected_ratio", None)
        return ("<span style='color:#bf8700;font-weight:700'>⚠️数值未交叉验证(" +
                html.escape(str(candidate)) + ")· 待人工确认,勿据此执行</span>")
    if display:
        return html.escape(display)
    parts = []
    amt = getattr(g, "ack_value", None)
    if amt is None:
        amt = _pick(g, "amount")
    ratio = _pick(g, "ratio")
    if amt is not None:
        parts.append(f"金额 <b>${amt}</b>")
    if ratio:
        parts.append(f"比例 <b>{html.escape(str(ratio))}</b>")
    return " · ".join(parts)


def _fmt_key_dates(g):
    """关键日列:宣告 / 除息除权 / 登记 / 派发,缺的标 —。"""
    if g.etype == "filing":
        return ""
    decl = _pick(g, "declaration_date")
    ex = g.anchor_date
    rec = _pick(g, "record_date")
    pay = _pick(g, "pay_date")
    first = getattr(g, "first_announced", None)

    def cell(label, val, color="#1f2328"):
        v = html.escape(str(val)) if val else "<span style='color:#bbb'>—</span>"
        return f"<span style='font-size:11px;color:#888'>{label}</span> <span style='color:{color}'>{v}</span>"

    rows = [cell("首发(公告)", first, "#0969da")]
    if not decl or decl != first:
        rows.append(cell("宣告", decl))
    rows += [cell("除息/除权", ex, "#cf222e"), cell("登记", rec), cell("派发", pay)]
    return "<br>".join(rows)



def _val_html(x):
    """金额/比例门禁(官网):未确认冲突 / 单源未交叉验证 → 一律不给确定值。"""
    if x.get("disputed") and not x.get("acked"):
        vals = x.get("dispute_vals") or {}
        pairs = " / ".join(str(v) for v in dict.fromkeys(vals.values()))
        return ("<span style='color:#cf222e;font-weight:700'> ⚠️各源不一致(" + html.escape(pairs)
                + ")· 待人工确认,勿据此执行</span>")
    unverified = x.get("value_verified") is False or (
        "value_verified" not in x and not x.get("official") and not x.get("acked")
        and (x.get("amt_srcs") or 0) == 1
    )
    if unverified and (x.get("amount") is not None or x.get("ratio")):
        v = x.get("amount") if x.get("amount") is not None else x.get("ratio")
        return ("<span style='color:#bf8700;font-weight:700'> ⚠️单源未交叉验证(" + html.escape(str(v))
                + ")· 待人工确认,勿据此执行</span>")
    if x.get("value_display"):
        return " " + html.escape(str(x["value_display"]))
    if x.get("amount") is not None:
        return html.escape(f" ${x['amount']}")
    if x.get("ratio"):
        return html.escape(f" {x['ratio']}")
    return ""


def _risk_html(x):
    risks = x.get("risk", []) if isinstance(x, dict) else getattr(x, "risk", [])
    return "".join(
        f"<br><span style='color:#9a6700'>⚠️ {html.escape(str(risk))}</span>"
        for risk in risks
    )

def build_dashboard(all_groups, source_health, alerts, meta):
    today = _business_date(meta)
    rows_html = []
    visible = _visible_alert_items(alerts)

    # 未来事件(分红/拆股/filing),按日期升序
    upcoming = []
    for tk, groups in all_groups.items():
        for g in groups:
            if (g.is_future and not
                    (g.etype == "filing" and getattr(g, "filing_relevant", None) is False)):
                upcoming.append(g)
    upcoming.sort(key=lambda g: g.anchor_date or "")

    for g in upcoming:
        days = g.days_to
        urgent = days is not None and days <= 7
        date_cell = f"<b>{g.anchor_date}</b><br><span style='color:#666'>D-{days}</span>"
        if urgent:
            date_cell = f"<b style='color:#cf222e'>{g.anchor_date}</b><br><span style='color:#cf222e'>D-{days}</span>"
        srcs = ", ".join(sorted(g.by_source.keys()))
        is_forecast = getattr(g, "forecast", False)
        fields = ("<span style='color:#bf8700;font-weight:700'>🔎预测观察 · 不执行</span>"
                  if is_forecast else _fmt_event_fields(g))
        fields += _risk_html(g)
        fields += _reference_html(g)
        status = "预测观察(不执行)" if is_forecast else STATUS_CN.get(g.status, g.status)
        status_color = "#9a6700" if is_forecast else STATUS_COLOR.get(g.status)
        conf = ""
        if g.conflicts:
            conf = "<br>".join("⚠ " + html.escape(c) for c in g.conflicts)
        rows_html.append(f"""
        <tr style="background:{'#fff8e1' if is_forecast else STATUS_BG.get(g.status,'#fff')}">
          <td>{date_cell}</td>
          <td><b>{html.escape(g.ticker)}</b><br><span style='color:#666;font-size:12px'>{html.escape(C.NAMES.get(g.ticker,''))}</span></td>
          <td>{_etype_label(g)}</td>
          <td>{fields}</td>
          <td style="line-height:1.7">{_fmt_key_dates(g)}</td>
          <td><span style="color:{status_color};font-weight:600">{status}</span></td>
          <td style="font-size:12px;color:#444">{html.escape(srcs)}</td>
          <td style="font-size:12px;color:#cf222e">{conf}</td>
        </tr>""")

    # 报警区
    def alert_block(title, items, render):
        if not items:
            return f"<h3>{title} <span style='color:#1a7f37'>· 0</span></h3><p style='color:#888'>无</p>"
        lis = "".join(f"<li>{render(x)}</li>" for x in items)
        return f"<h3>{title} <span style='color:#cf222e'>· {len(items)}</span></h3><ul>{lis}</ul>"

    def _new_render(g):
        base = f"<b>{g.ticker}</b> {_etype_label(g)} {g.anchor_date} ({_fmt_event_fields(g) or g.note})"
        u = _sec_url(g) if g.etype == "filing" else ""
        if u:
            base += f" <a href='{html.escape(u)}' target='_blank' rel='noopener'>原文 ↗</a>"
        base += _risk_html(g)
        base += _reference_html(g)
        return base
    new_html = alert_block("🆕 新发现事件", visible["new"], _new_render)

    # 已确认未来事项持续展示；“展示”不等于合约需要操作。
    def _pending_render(x):
        prod = ""
        if x.get("products"):
            prod = "<span style='background:#eef;color:#3538cd;border-radius:4px;padding:0 6px;font-size:12px'>" \
                   + "+".join(x["products"]) + "</span> "
        val = _val_html(x)
        # 关键日链:首发 · 宣告 · 登记 · 除息/生效 · 派发(纯文本,外层会 html.escape)
        dates = ""
        if x.get("first"):
            dates += f"首发 {x['first']} · "
        if x.get("decl"):
            dates += f"宣告 {x['decl']} · "
        if x.get("record"):
            dates += f"登记 {x['record']} · "
        dates += f"{'除息' if x.get('etype') == 'dividend' else '生效'} {x['date']}"
        if x.get("pay"):
            dates += f" · 派发 {x['pay']}"
        risk = _risk_html(x)
        ref = _reference_html(x)
        tip = ""
        if x.get("days") is not None and x.get("follow_up_mode") == "execution":
            tip = f"<br><span style='color:#0969da'>👉 {html.escape(C.alert_copy(x['days']))}</span>"
        elif x.get("follow_up_mode") == "verification":
            tip = "<br><span style='color:#bf8700'>👉 只核验合约门槛；未确认前不执行调整。</span>"
        elif x.get("follow_up_mode") == "none":
            tip = "<br><span style='color:#1a7f37'>👉 仅信息展示，不进入合约周期催办。</span>"
        action_status = (x.get("contract_action") or {}).get("status")
        countdown_color = "#1a7f37" if action_status == "not_required" and x.get("follow_up_mode") == "none" else (
            "#bf8700" if x.get("follow_up_mode") == "verification" else "#cf222e"
        )
        return (f"{prod}<b>{x['ticker']}</b> {_etype_label(x)}{val} — "
                f"<b style='color:{countdown_color}'>还剩 {x['days']} 天</b>　<span style='color:#555;font-size:12px'>{html.escape(dates)}</span>{tip}{risk}{ref}")
    pending_html = alert_block("📋 已确认未来事项(含合约无需操作；仅需处理/核验项进入 30/14 日提醒)", alerts.get("pending", []), _pending_render)

    # 🔎 单源且无宣告日 = 预测观察：保留追踪，并按 30/14 天推核验提醒；
    # 绝不进入正式执行催办。
    def _forecast_render(x):
        prod = ("<span style='background:#fff8c5;color:#7a4b00;border-radius:4px;padding:0 6px;font-size:12px'>"
                + "+".join(x["products"]) + "</span> ") if x.get("products") else ""
        dates = " · ".join(p for p in [
            f"登记 {x['record']}" if x.get("record") else "",
            f"{'除息' if x.get('etype') == 'dividend' else '生效'} {x.get('date')}",
            f"派发 {x['pay']}" if x.get("pay") else "",
        ] if p)
        srcs = html.escape(", ".join(x.get("srcs") or []) or "未知")
        note = f"<br><span style='color:#555'>📝 {html.escape(x['watch_note'])}</span>" if x.get("watch_note") else ""
        return (f"{prod}<b>{html.escape(x['ticker'])}</b> {_etype_label(x)} — "
                f"<b style='color:#bf8700'>🔎 预测观察 · 数值待核实 · 不执行</b>　"
                f"<span style='color:#555;font-size:12px'>{html.escape(dates)}</span>"
                f"<br><span style='color:#555'>📡 数据源:{srcs}；按 30/14 天节奏推核验提醒；"
                f"确认后转正式事项，再按现货流程/合约 3% 结论决定是否催办</span>{note}{_reference_html(x)}")
    forecast_html = alert_block("🔎 待核实预测(会推核验提醒 · 未确认勿执行)", alerts.get("forecasts", []), _forecast_render)

    def _forecast_update_render(x):
        if x.get("kind") in ("promoted", "declared"):
            why = (("公司官方宣告" if x.get("official") else "已获取宣告日")
                   if x.get("kind") == "declared" else "第二个独立源已确认")
            return (f"✅ <b>{html.escape(x['ticker'])}</b> 预测已转正式：{why}，除息/生效 "
                    f"{html.escape(str(x.get('date')))}{_risk_html(x)}{_reference_html(x)}")
        if x.get("kind") == "updated":
            return (f"🔄 <b>{html.escape(x['ticker'])}</b> 预测更新：日期 {html.escape(str(x.get('previous_date') or '—'))} → "
                    f"{html.escape(str(x.get('date')))}")
        return (f"❌ <b>{html.escape(x['ticker'])}</b> 预测失效：预计日 "
                f"{html.escape(str(x.get('date')))} 已过仍未证实，不执行。{_reference_html(x)}")
    forecast_updates_html = alert_block("🔄 预测状态更新(自动追踪)", visible["forecast_updates"], _forecast_update_render)

    def _contract_update_render(x):
        labels = {
            "required": "现已达到 >3% 合约操作门槛",
            "not_required": "已确认合约本次无需操作",
            "review": "转为合约门槛待核实",
        }
        return (f"🔄 <b>{html.escape(x['ticker'])}</b> {_etype_label(x)} "
                f"{html.escape(str(x.get('date')))}："
                f"{html.escape(labels.get(x.get('current_status'), x.get('current_status') or '结论更新'))}"
                f"{_risk_html(x)}")

    contract_updates_html = alert_block(
        "🔄 合约操作结论更新", visible["contract_updates"], _contract_update_render
    )

    # 📣 新公告(刚扫到 declaration date)
    def _ann_render(x):
        prod = ""
        if x.get("products"):
            prod = "<span style='background:#eef;color:#3538cd;border-radius:4px;padding:0 6px;font-size:12px'>" \
                   + "+".join(x["products"]) + "</span> "
        val = _val_html(x)
        days = f" · <b style='color:#cf222e'>还剩 {x['days']} 天</b>" if x.get("days") is not None else ""
        prefix = "✅ 预测已转正式 · " if x.get("forecast_watch") else ""
        return (f"{prefix}{prod}<b>{x['ticker']}</b> {_etype_label(x)}{val} — "
                f"<span style='color:#0969da'>宣告 {x.get('decl')}</span> · 除息 {x['date']}{days}"
                f"{_risk_html(x)}{_reference_html(x)}")
    # 网页报警去重:已在「临近预警(催办)」里的事件,不在「新公告」重复;「待执行」整块由时间线覆盖,不再单列
    announced_html = alert_block("📣 新公告(刚宣告)", visible["announced"], _ann_render)
    def _round_dates(x):
        bits = []
        if x.get("decl"): bits.append(f"宣告 {x['decl']}")
        if x.get("record"): bits.append(f"登记 {x['record']}")
        bits.append(f"{'除息' if x.get('etype') == 'dividend' else '生效'} {x['date']}")
        if x.get("pay"): bits.append(f"派发 {x['pay']}")
        return " · ".join(html.escape(b) for b in bits)
    def _round_render(x):
        prod = ("[" + "+".join(x["products"]) + "] ") if x.get("products") else ""
        s = (f"{prod}<b>{x['ticker']}</b> {_etype_label(x)} — "
             f"<b style='color:#cf222e'>D-{x['days']}</b> ({x['round']}天轮)"
             f"<br><span style='font-size:12px;color:#555'>{_round_dates(x)}</span>")
        if x.get("ops"):
            s += f"<br><span style='color:#0969da'>👉 {html.escape(x['ops'])}</span>"
        if x.get("risk_copy"):
            s += f"<br><span style='color:#9a6700'>🛡 {html.escape(x['risk_copy'])}</span>"
        return s
    round_html = ""  # 「临近预警」已并入上面的「临近催办 · 待执行」,网页不再单列
    # 零容忍:不做口径豁免。挂着就一直报,只有人工「确认」能消解 —— 挂越久标记越醒目。
    _rv = alerts.get("review") or {}
    _esc = _rv.get("escalate_days", 3)

    def _aged(g):
        a = getattr(g, "age_days", 0) or 0
        if a >= _esc:
            return (f" <span style='color:#cf222e;font-weight:700'>⏳已挂 {a} 天未确认</span>")
        return f" <span style='color:#888;font-size:12px'>已挂 {a} 天</span>" if a else ""

    def _adr_html(g):  # ADR 预扣税提示:保证认税前毛额
        n = R.adr_tax_note(g.ticker, g.by_source) if g.etype == "dividend" else ""
        n = n.replace("**", "")
        return (f"<br><span style='color:#cf222e'>{html.escape(n)}</span>") if n else ""
    conf_html = alert_block("❗ 字段冲突(零容忍 · 需人工确认)", alerts["conflicts"],
        lambda g: f"<b>{g.ticker}</b> {_etype_label(g)} {g.anchor_date}: "
                  + "; ".join(html.escape(c) for c in g.conflicts) + _aged(g) + _adr_html(g)
                  + _risk_html(g) + _reference_html(g))
    gap_html = alert_block("🕳 数据空缺(需人工确认)", alerts["gaps"],
        lambda g: f"<b>{g.ticker}</b> {_etype_label(g)} {g.anchor_date}: "
                  + "; ".join(html.escape(x) for x in g.gaps) + _aged(g))

    # 顶部「待人工确认」横幅:不确认就一直在
    # 规则说明(常驻):让看网页的人知道「为什么有些金额不给数字」「怎么解除」
    rules_html = (
        "<details style='background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;padding:10px 14px;margin:12px 0'>"
        "<summary style='cursor:pointer;font-weight:600'>📖 规则说明:取值口径 · 金额门禁 · 预测观察/人工确认(点开)</summary>"
        "<div style='font-size:13px;color:#444;line-height:1.9;margin-top:8px'>"
        "<b>取值</b>:金额/比例取<b>多数票 + 源优先级</b>(要的是公司宣告的<b>原值</b>)。各源口径不同——"
        "yfinance 会按拆股回溯调整历史分红、还四舍五入;Alpaca 对 ADR 报的是<b>扣预扣税后的净额</b>"
        "(历史 ADR 案例：ASML=gross×0.85 荷兰15%、TSM×0.79 台湾21%)。<br>"
        "<b>🚦 金额门禁</b>:只有<b>多源交叉验证过且无冲突</b>的金额才显示确定值,否则一律封锁:<br>"
        "　• <span style='color:#cf222e;font-weight:600'>⚠️各源不一致(a / b)</span> —— 源之间对不上<br>"
        "　• <span style='color:#bf8700;font-weight:600'>⚠️单源未交叉验证(x)</span> —— 只有 1 个源报,没交叉验证过<br>"
        "　• <span style='color:#bf8700;font-weight:600'>🔎预测观察</span> —— 单源且未见宣告日的预估;"
        "进入 30 天窗口知会一次、14 天内每日推数据核验提醒，但不进入正式执行催办。"
        "可发 <code>观察 代码 日期 [备注]</code> 标记重点跟踪;改期/升级/失效也会主动推送。<br>"
        "<b>预测值不得据此执行。</b><br>"
        "<b>合约操作门槛</b>:公司行动照常展示；现金分红按每股毛额÷前一完整交易日未调整收盘价估算，"
        "现金、送股、拆股、合股等只有估算价格影响严格超过 3% 才需操作，3% 或以下明确标记“合约：本次无需操作”。"
        "缺金额、比例或参考价时只标待核实，不会武断判定无需操作。<br>"
        "<b>🙋 人工介入(零容忍·不豁免)</b>:字段冲突和数据空缺每次扫描都重报、一直挂着并显示「已挂 N 天」,"
        "超 3 天没人确认会在推送里 @ 负责人。消解方式:群里发 "
        "<code>确认 代码 [正确值]</code>(如 <code>确认 AAPL 0.26</code>)"
        "→ 即时写入生效库并标记异常；金额/比例门禁及 3% 产品结论由下一轮流水线按币种、"
        "证券单位和参考价重算，资料不足时仍保留核验提醒，并完整留痕(谁确认、何时)。"
        "</div></details>")

    review_html = ""
    if _rv.get("open"):
        od = _rv.get("overdue", 0)
        bg, bd = ("#fff5f5", "#cf222e") if od else ("#fffbe6", "#bf8700")
        over = (f"<br><b style='color:#cf222e'>其中 {od} 条已超过 {_esc} 天没人确认(推送会 @ 负责人)</b>"
                if od else "")
        review_html = (
            f"<div style='background:{bg};border-left:4px solid {bd};padding:12px 14px;border-radius:6px;margin:12px 0'>"
            f"<b style='font-size:15px'>🙋 待人工确认 {_rv['open']} 条</b>"
            f"　<span style='color:#555'>字段冲突 {_rv.get('conflicts',0)} · 数据空缺 {_rv.get('gaps',0)}"
            f"　最久已挂 <b>{_rv.get('max_age',0)}</b> 天</span>{over}"
            f"<div style='color:#555;font-size:13px;margin-top:6px'>零容忍:<b>不做口径豁免</b>。"
            f"核对后在群里发 <code>确认 代码 [正确值]</code>(例:<code>确认 AAPL 0.26</code>)才会消解;"
            f"不确认则每次扫描都会继续报。</div></div>")

    def _resolved_render(r):
        v = f" · 以 <b>{html.escape(str(r['value']))}</b> 为准" if r.get("value") else ""
        meta_line = f"原冲突:{html.escape(r.get('detail',''))}"
        if r.get("at"):
            meta_line += f" · 确认于 {html.escape(str(r['at']))}"
        return (f"<b>{html.escape(r['ticker'])}</b> {_etype_label(r)} {r['date']}{v}"
                f"<br><span style='font-size:12px;color:#555'>{meta_line}</span>")
    resolved_items = alerts.get("resolved", [])
    resolved_html = (f"<h3>✅ 已人工确认(finalize) <span style='color:#1a7f37'>· {len(resolved_items)}</span></h3>"
                     + "<ul>" + "".join(f"<li>{_resolved_render(r)}</li>" for r in resolved_items) + "</ul>"
                     ) if resolved_items else ""

    # 源健康矩阵
    sources_order = ["yfinance", "FMP", "AlphaVantage", "Nasdaq", "Tiingo", "Alpaca", "SEC", "FINX"]
    health_rows = []
    for tk in C.TICKERS:
        cells = []
        for s in sources_order:
            st = source_health.get(tk, {}).get(s, "—")
            color = {"ok": "#1a7f37", "unavailable": "#cf222e"}.get(st, "#bbb")
            mark = {"ok": "●", "unavailable": "○"}.get(st, "·")
            cells.append(f"<td style='text-align:center;color:{color}' title='{st}'>{mark}</td>")
        health_rows.append(f"<tr><td><b>{tk}</b></td>{''.join(cells)}</tr>")
    health_head = "".join(f"<th style='font-size:11px'>{s}</th>" for s in sources_order)

    # 资产覆盖表
    _TYPE_CN = {"equity": "个股", "etf": "ETF", "commodity": "商品/外汇", "foreign": "海外股"}
    def _yn(b):
        return "<span style='color:#1a7f37'>✓</span>" if b else "<span style='color:#ccc'>—</span>"
    cov_rows = []
    n_spot = n_contract = n_mon = 0
    for tk in C.ALL_ASSETS:
        spot = tk in C.SPOT_TICKERS; contract = tk in C.CONTRACT_TICKERS
        mon = C.is_monitored(tk)
        n_spot += spot; n_contract += contract; n_mon += mon
        mon_cell = ("<span style='color:#1a7f37'>已监控</span>" if mon
                    else "<span style='color:#9a6700'>不适用</span>")
        cov_rows.append(
            f"<tr><td><b>{html.escape(tk)}</b></td>"
            f"<td style='color:#666;font-size:12px'>{html.escape(C.NAMES.get(tk,''))}</td>"
            f"<td>{_yn(spot)}</td><td>{_yn(contract)}</td>"
            f"<td style='font-size:12px'>{_TYPE_CN.get(C.asset_type(tk), C.asset_type(tk))}</td>"
            f"<td style='font-size:12px'>{mon_cell}</td></tr>")
    n_assets = len(C.ALL_ASSETS)

    # 更新日志
    chg = load_changelog()
    chg_html = _changelog_entries_html(chg)

    # 参考链接维护台(refs.json 的 ir_dividend):分红核对链接的人工维护源
    _refs_ir = load_refs()
    ref_rows = []
    for tk in sorted(_refs_ir):
        u = _refs_ir.get(tk) or ""
        cell = (f"<a href='{html.escape(u)}' target='_blank' rel='noopener'>IR 分红页 ↗</a>"
                if u else "<span style='color:#9a6700'>未维护 → 回退 SEC 公司备案 + 第三方交叉核对</span>")
        ref_rows.append(f"<tr><td><b>{html.escape(tk)}</b></td><td>{cell}</td></tr>")
    ref_html = ("".join(ref_rows) if ref_rows
                else "<tr><td colspan='2' style='color:#888'>refs.json 暂无条目</td></tr>")

    # ---- SEC 原文(近期 filing 类公司行动文件)----
    today_s = today.isoformat()
    cutoff_s = (today - dt.timedelta(days=90)).isoformat()
    filings = []
    for tk, groups in all_groups.items():
        for g in groups:
            if g.etype == "filing" and (g.anchor_date or "") >= cutoff_s:
                filings.append(g)
    filings.sort(key=lambda g: g.anchor_date or "", reverse=True)
    PER_PAGE = 30
    sec_rows = []
    for idx, g in enumerate(filings):
        sec = g.by_source.get("SEC") or {}
        u = sec.get("url", "") or _sec_url(g)
        accepted = sec.get("accepted", "")
        relevant = sec.get("relevant", False)
        link = (f"<a href='{html.escape(u)}' target='_blank' rel='noopener'>查看原文 ↗</a>"
                if u else "<span style='color:#bbb'>—</span>")
        rel_cell = ("<span style='background:#fff1f0;color:#cf222e;border-radius:4px;padding:1px 6px'>公司行动相关</span>"
                    if relevant else "<span style='color:#999'>一般</span>")
        acc = f"<br><span style='color:#aaa;font-size:11px'>{html.escape(accepted)}</span>" if accepted else ""
        hide = " style='display:none'" if idx >= PER_PAGE else ""
        sec_rows.append(
            f"<tr class='secrow'{hide}><td>{g.anchor_date}{acc}</td>"
            f"<td><b>{html.escape(g.ticker)}</b> "
            f"<span style='color:#888;font-size:12px'>{html.escape(C.NAMES.get(g.ticker,''))}</span></td>"
            f"<td>{html.escape(g.note or '')}</td><td>{rel_cell}</td><td>{link}</td></tr>")
    n_filings = len(filings)
    n_pages = max(1, (n_filings + PER_PAGE - 1) // PER_PAGE)
    pager = (f"""<div id="sec-pager" style="margin-top:10px;font-size:13px">
      <button onclick="secPage(-1)" class="pgbtn">上一页</button>
      <span id="sec-pageinfo" style="margin:0 10px">第 1 / {n_pages} 页(共 {n_filings} 条)</span>
      <button onclick="secPage(1)" class="pgbtn">下一页</button>
    </div>""" if n_pages > 1 else "")
    sec_table = (f"""
  <h2>📄 SEC 原文(近 90 天公司行动文件)</h2>
  <div class="sub2">「事件」来自 8-K 的 Item 细分;标「公司行动相关」的多与并购/退市/分红等需发公告事项有关。</div>
  <table id="sec-table">
    <tr><th>申报日 / 时刻</th><th>标的</th><th>事件(8-K Item)</th><th>相关</th><th>原文</th></tr>
    {''.join(sec_rows)}
  </table>
  {pager}""" if sec_rows else "")

    n_conf = len(alerts["conflicts"]); n_gap = len(alerts["gaps"])
    n_new = len(visible["new"]); n_round = len(visible["rounds"])
    n_upcoming = len(upcoming)

    body = f"""
  <div class="cards">
    <div class="card"><div class="n">{n_upcoming}</div><div class="l">未来事件</div></div>
    <div class="card"><div class="n" style="color:#0969da">{n_new}</div><div class="l">新发现</div></div>
    <div class="card"><div class="n" style="color:#9a6700">{n_round}</div><div class="l">临近提醒</div></div>
    <div class="card"><div class="n" style="color:#cf222e">{n_conf}</div><div class="l">字段冲突</div></div>
    <div class="card"><div class="n" style="color:#cf222e">{n_gap}</div><div class="l">数据空缺</div></div>
  </div>

  <div class="legend">
    <span><span class="dot" style="background:#1a7f37"></span>已确认(≥2源一致)</span>
    <span><span class="dot" style="background:#9a6700"></span>单源待核实</span>
    <span><span class="dot" style="background:#cf222e"></span>有冲突</span>
  </div>

  <h2>未来事件时间线</h2>
  <table>
    <tr><th>除息/除权日</th><th>标的</th><th>类型</th><th>详情</th><th>关键日期</th><th>核对状态</th><th>来源</th><th>冲突</th></tr>
    {''.join(rows_html) if rows_html else '<tr><td colspan=8 style="color:#888">暂无未来事件</td></tr>'}
  </table>

  <h2>报警</h2>
  {rules_html}
  {review_html}
  {round_html}
  {forecast_updates_html}
  {contract_updates_html}
  {announced_html}
  {pending_html}
  {forecast_html}
  {new_html}
  {conf_html}
  {resolved_html}
  {gap_html}
  {sec_table}

  <h2>数据源健康(●可用 ○不可用)</h2>
  <table>
    <tr><th>标的</th>{health_head}</tr>
    {''.join(health_rows)}
  </table>

  <h2>资产覆盖(现货 / 合约)</h2>
  <div class="sub2">现货 {n_spot} · 合约 {n_contract} · 共 {n_assets} 个资产(监控 {n_mon} · 不适用 {n_assets - n_mon})</div>
  <table>
    <tr><th>标的</th><th>名称</th><th>现货</th><th>合约</th><th>类型</th><th>监控</th></tr>
    {''.join(cov_rows)}
  </table>"""
    return body


def build_changelog_panel(meta):
    """独立「更新日志」面板:更新日志 + 参考链接维护台。"""
    chg = load_changelog()
    chg_html = _changelog_entries_html(chg)

    refs = load_refs()
    ref_rows = []
    for tk in sorted(refs):
        u = refs.get(tk) or ""
        cell = (f"<a href='{html.escape(u)}' target='_blank' rel='noopener'>IR 分红页 ↗</a>"
                if u else "<span style='color:#9a6700'>未维护 → 回退 SEC 公司备案 + 第三方交叉核对</span>")
        ref_rows.append(f"<tr><td><b>{html.escape(tk)}</b></td><td>{cell}</td></tr>")
    ref_html = "".join(ref_rows) if ref_rows else "<tr><td colspan='2' style='color:#888'>refs.json 暂无条目</td></tr>"

    return f"""
  <h2>🆕 更新日志</h2>
  <div class="sub2">每次发版的改动记录(最新在前)。来源:仓库 CHANGELOG.md。</div>
  {chg_html}

  <h2>🔗 参考链接维护台</h2>
  <div class="sub2">分红核对链接统一由 <code>run.py</code> 下发：已核验的官方本次公告/IR → 宣告 8-K → 下表公司 IR 分红页 → SEC 公司备案，并始终附 StockAnalysis 交叉核对（可能滞后）。维护官方页请编辑 <code>refs.json</code> 的 <code>ir_dividend</code>；仅已逐项核验的官方事件填 <code>official_event_overrides</code>。</div>
  <table>
    <tr><th>标的</th><th>IR 分红页</th></tr>
    {ref_html}
  </table>"""


def build_text_digest(alerts, meta):
    """定时推送用的纯文本预警清单。"""
    L = [f"【公司行动预警】{meta['generated']}", ""]
    visible = _visible_alert_items(alerts)
    def sec(title, items, fmt):
        L.append(f"== {title} ({len(items)}) ==")
        if not items:
            L.append("  无")
        for x in items:
            L.append("  • " + fmt(x))
        L.append("")
    def _round_line(x):
        prod = ("[" + "+".join(x["products"]) + "] ") if x.get("products") else ""
        lab = "除息" if x.get("etype") == "dividend" else "生效"
        prefix = ("[单源核验·勿执行] " if x.get("forecast") else
                  "[合约门槛核验·勿执行] " if x.get("verification") else
                  "[单源已转正式] " if x.get("promoted_from_forecast") else "[正式催办] ")
        s = (f"{prefix}{prod}{x['ticker']} {_etype_label(x)} D-{x['days']} |"
             + (f" 宣告 {x['decl']}" if x.get('decl') else "")
             + (f" 登记 {x['record']}" if x.get('record') else "")
             + f" {lab} {x['date']}"
             + (f" 派发 {x['pay']}" if x.get('pay') else ""))
        if x.get("ops"):
            s += f"\n      👉 {x['ops']}"
        for risk in x.get("risk", []):
            s += f"\n      ⚠️ {risk}"
        return s
    sec("临近提醒(执行催办 + 数据/合约门槛核验；非本周清单；≤14天每天 · 30天知会)", visible["rounds"], _round_line)

    # 优先级互斥去重:催办 > 新公告 > 待执行
    def _sig(x):
        return _event_key(x)
    _claimed = {_sig(x) for x in visible["rounds"]}
    _promotion_round_sigs = {
        _sig(x) for x in visible["rounds"] if x.get("promoted_from_forecast")
    }
    _ann = visible["announced"]
    for x in _ann:
        _claimed.add(_sig(x))
    _pend = [x for x in alerts.get("pending", []) if _sig(x) not in _claimed]

    def _ann_line(x):
        prod = ("[" + "+".join(x["products"]) + "] ") if x.get("products") else ""
        val = _val_html(x)
        d = f" 还剩{x['days']}天" if x.get("days") is not None else ""
        s = f"{prod}{x['ticker']} {_etype_label(x)}{val} 宣告 {x.get('decl')} · 除息 {x['date']}{d}"
        for risk in x.get("risk", []):
            s += f"\n      ⚠️ {risk}"
        return s
    sec("新公告(刚宣告)", _ann, _ann_line)

    def _pending_line(x):
        prod = ("[" + "+".join(x["products"]) + "] ") if x.get("products") else ""
        val = _val_html(x)
        s = f"{prod}{x['ticker']} {_etype_label(x)}{val} 还剩{x['days']}天 · 除息 {x['date']}"
        if x.get("record"):
            s += f" 登记 {x['record']}"
        if x.get("pay"):
            s += f" 派发 {x['pay']}"
        for r in x.get("risk", []):
            s += f"\n      ⚠️ {r}"
        return s
    sec("已确认未来事项(含合约无需操作)", _pend, _pending_line)

    def _forecast_line(x):
        prod = ("[" + "+".join(x["products"]) + "] ") if x.get("products") else ""
        lab = "除息" if x.get("etype") == "dividend" else "生效"
        return (f"{prod}{x['ticker']} {_etype_label(x)} 数值待核实 | "
                f"{lab} {x.get('date')} · 不执行;等待公司宣告或第二个独立源")
    _forecast_rest = [x for x in alerts.get("forecasts", []) if _sig(x) not in _claimed]
    sec("其余待核实预测(临近会推核验提醒 · 未确认勿执行)", _forecast_rest, _forecast_line)

    def _forecast_update_line(x):
        if x.get("kind") in ("promoted", "declared"):
            why = (("公司官方宣告" if x.get("official") else "已获取宣告日")
                   if x.get("kind") == "declared" else "第二个独立源已确认")
            return (f"{x['ticker']} 预测已转正式：{why}，除息/生效 {x.get('date')}" +
                    "".join(f"\n      ⚠️ {risk}" for risk in x.get("risk", [])))
        if x.get("kind") == "updated":
            return f"{x['ticker']} 预测更新：日期 {x.get('previous_date') or '—'} → {x.get('date')}"
        return f"{x['ticker']} 预测失效：预计日 {x.get('date')} 已过仍未证实，不执行"
    _forecast_updates_rest = [
        x for x in visible["forecast_updates"] if _sig(x) not in _promotion_round_sigs
    ]
    sec("预测状态更新(自动追踪)", _forecast_updates_rest, _forecast_update_line)

    _forecast_update_sigs = {_sig(x) for x in _forecast_updates_rest}
    _contract_updates_rest = visible["contract_updates"]
    def _contract_update_line(x):
        labels = {
            "required": "现已达到 >3% 合约操作门槛",
            "not_required": "已确认合约本次无需操作",
            "review": "转为合约门槛待核实",
        }
        return (f"{x['ticker']} {x.get('date')}：{labels.get(x.get('current_status'), '结论更新')}" +
                "".join(f"\n      ⚠️ {risk}" for risk in x.get("risk", [])))
    sec("合约操作结论更新", _contract_updates_rest, _contract_update_line)
    sec("新发现事件", visible["new"],
        lambda g: (f"{g.ticker} {_etype_label(g)} {g.anchor_date} {(_strip(g))}" +
                   "".join(f"\n      ⚠️ {risk}" for risk in getattr(g, "risk", []))))
    sec("字段冲突(零容忍·需人工确认)", alerts["conflicts"],
        lambda g: (f"{g.ticker} {_etype_label(g)} {g.anchor_date}: " + "; ".join(g.conflicts)
                   + "".join(f"\n      ⚠️ {risk}" for risk in getattr(g, "risk", []))))
    sec("数据空缺(需人工确认)", alerts["gaps"],
        lambda g: f"{g.ticker} {_etype_label(g)} {g.anchor_date}: " + "; ".join(g.gaps))
    return "\n".join(L)


def _strip(g):
    if g.etype == "filing":
        return g.note or ""
    if R.is_disputed(g):
        return "⚠️各源不一致·待确认"
    if not getattr(g, "value_verified", False):
        return "⚠️数值未交叉验证·待确认"
    if getattr(g, "value_display", ""):
        return g.value_display
    amt = _pick(g, "amount")
    ratio = _pick(g, "ratio")
    return (f"${amt}" if amt is not None else "") + (f" {ratio}" if ratio else "")


# ==================== 月历视图 ====================
# 类型配色
CAL_TYPE = {
    "dividend": {"bg": "#dbeafe", "fg": "#1e40af", "label": "分红"},
    "split":    {"bg": "#ede9fe", "fg": "#6d28d9", "label": "拆股"},
    "filing":   {"bg": "#ffedd5", "fg": "#c2410c", "label": "并购/退市"},
}
# 关键日类型(同一事件铺到不同日子)
KIND_MARK = {"ex": "除", "record": "登", "pay": "派"}


def _collect_calendar_marks(all_groups, start, end):
    """把事件展开成 {date: [mark,...]}。每个分红/拆股事件铺 3 个关键日(除/登/派)。"""
    marks = {}

    def add(date_s, m):
        if not date_s or not (start.isoformat() <= date_s <= end.isoformat()):
            return
        marks.setdefault(date_s, []).append(m)

    for tk, groups in all_groups.items():
        for g in groups:
            if g.etype == "filing":
                note = (g.note or "")
                # 统一消费 run.py 的三态判定；普通备案留在 SEC 原文表，英文或
                # 未知结构性行动继续显示并明确待核实。
                if getattr(g, "filing_relevant", None) is not False:
                    add(g.anchor_date, {"tk": g.ticker, "kind": "ex", "etype": "filing",
                                        "status": g.status, "text": note[:18],
                                        "tip": (f"{g.ticker} {note}(点击看 SEC 原文) | " +
                                                " · ".join(getattr(g, "risk", []))),
                                        "url": _sec_url(g)})
                continue
            amt = getattr(g, "selected_amount", None)
            ratio = getattr(g, "selected_ratio", None)
            ex, rec, pay = g.anchor_date, _pick(g, "record_date"), _pick(g, "pay_date")
            decl = _pick(g, "declaration_date")
            first = getattr(g, "first_announced", None)
            # 门禁:未确认冲突 / 单源未交叉验证 → 格子里不给确定数字
            if getattr(g, "forecast", False):
                val = "🔎预测"
            elif R.is_disputed(g):
                val = "⚠️待确认"
            elif (not getattr(g, "value_verified", False) and
                  (amt is not None or ratio is not None)):
                val = "⚠️单源"
            else:
                val = getattr(g, "value_display", "") or (
                    (f"${amt}" if amt is not None else "") + (f" {ratio}" if ratio else "")
                )
            forecast_tip = " · 预测观察(未证实，不执行)" if getattr(g, "forecast", False) else ""
            action_tip = " · ".join(getattr(g, "risk", []))
            tip = (f"{g.ticker} {_etype_label(g)} {val}{forecast_tip} | 首发 {first or '—'} · "
                   f"宣告 {decl or '—'} · 除息 {ex or '—'} · 登记 {rec or '—'} · 派发 {pay or '—'} | "
                   f"{STATUS_CN.get(g.status)}" + (f" | {action_tip}" if action_tip else ""))
            add(ex, {"tk": g.ticker, "kind": "ex", "etype": g.etype, "status": g.status,
                     "text": f"{val}", "tip": tip,
                     "url": getattr(g, "primary_url", "") if g.etype == "dividend" else ""})
            add(rec, {"tk": g.ticker, "kind": "record", "etype": g.etype, "status": g.status,
                      "text": "", "tip": tip,
                      "url": getattr(g, "primary_url", "") if g.etype == "dividend" else ""})
            add(pay, {"tk": g.ticker, "kind": "pay", "etype": g.etype, "status": g.status,
                      "text": "", "tip": tip,
                      "url": getattr(g, "primary_url", "") if g.etype == "dividend" else ""})
    return marks


# 关键日的中文标签
KIND_LABEL = {
    "record": "登记日", "pay": "派发日",
    "ex": {"dividend": "除息", "split": "除权·生效", "filing": "公告"},
}


def _render_month(year, month, marks, today):
    _cal.setfirstweekday(0)  # 周一起始
    weeks = _cal.monthcalendar(year, month)
    head = "".join(f"<th>{d}</th>" for d in ["一", "二", "三", "四", "五", "六", "日"])
    body = []
    for wk in weeks:
        tds = []
        for day in wk:
            if day == 0:
                tds.append("<td class='empty'></td>")
                continue
            ds = dt.date(year, month, day).isoformat()
            is_today = (ds == today.isoformat())
            # 主事件(除息/公告)排前,登记/派发在后
            order = {"ex": 0, "record": 1, "pay": 2}
            day_marks = sorted(marks.get(ds, []), key=lambda m: (order[m["kind"]], m["tk"]))
            pills = []
            for m in day_marks:
                col = CAL_TYPE[m["etype"]]
                ring = (";box-shadow:0 0 0 2px #cf222e inset" if m["status"] == "conflict"
                        else ";box-shadow:0 0 0 2px #d4a72c inset" if m["status"] == "single" else "")
                tip = html.escape(m["tip"])
                if m["kind"] == "ex":
                    if m["etype"] == "filing":
                        inner = (f"<span class='tk'>{html.escape(m['tk'])}</span>"
                                 f"<span class='ty'>{html.escape(m['text'])}</span>")
                    else:
                        kd = KIND_LABEL["ex"][m["etype"]]
                        val = f"<span class='val'>{html.escape(m['text'])}</span>" if m["text"] else ""
                        inner = (f"<span class='tk'>{html.escape(m['tk'])}</span>"
                                 f"<span class='ty'>{col['label']}</span>{val}"
                                 f"<span class='kd'>{kd}</span>")
                    pill = (f"<div class='pill ex' style='background:{col['bg']};"
                            f"color:{col['fg']};border-left:3px solid {col['fg']}{ring}' title='{tip}'>{inner}</div>")
                    u = m.get("url")
                    if u:
                        pill = f"<a href='{html.escape(u)}' target='_blank' rel='noopener' style='text-decoration:none'>{pill}</a>"
                    pills.append(pill)
                else:
                    lbl = KIND_LABEL[m["kind"]]
                    pill = (f"<div class='pill sub' style='color:{col['fg']}' title='{tip}'>"
                            f"<span class='tk'>{html.escape(m['tk'])}</span>"
                            f"<span class='sublbl'>{lbl}</span></div>")
                    u = m.get("url")
                    if u:
                        pill = f"<a href='{html.escape(u)}' target='_blank' rel='noopener' style='text-decoration:none'>{pill}</a>"
                    pills.append(pill)
            cls = "today" if is_today else ""
            tds.append(f"<td class='{cls}'><div class='dn'>{day}</div>{''.join(pills)}</td>")
        body.append("<tr>" + "".join(tds) + "</tr>")
    return f"""<div class="month">
      <h3>{year} 年 {month} 月</h3>
      <table class="cal"><tr>{head}</tr>{''.join(body)}</table>
    </div>"""


def build_calendar(all_groups, meta, months_ahead=3, lookback_days=15):
    """返回日历 body 片段(供合并站点使用)。"""
    today = _business_date(meta)
    start = today - dt.timedelta(days=lookback_days)
    end = today
    for _ in range(months_ahead):
        end = (end.replace(day=28) + dt.timedelta(days=10)).replace(day=1)
    end = end - dt.timedelta(days=1)

    marks = _collect_calendar_marks(all_groups, start, end)
    months, cur = [], today.replace(day=1)
    for _ in range(months_ahead + 1):
        months.append((cur.year, cur.month))
        cur = (cur.replace(day=28) + dt.timedelta(days=10)).replace(day=1)
    grids = "".join(_render_month(y, m, marks, today) for y, m in months)
    n_events = sum(1 for v in marks.values() for mk in v if mk["kind"] == "ex")

    return f"""
  <div class="legend">
    <b>类型</b>
    <span class="c" style="background:#dbeafe;color:#1e40af">分红</span>
    <span class="c" style="background:#ede9fe;color:#6d28d9">拆股</span>
    <span class="c" style="background:#ffedd5;color:#c2410c">并购/退市</span>
    &nbsp;&nbsp;<b>关键日</b> 主块=除息/除权(带金额) · <i>登记日</i> · <i>派发日</i>(浅色)
    &nbsp;&nbsp;<b>核对</b>
    <span class="rb" style="box-shadow:0 0 0 2px #cf222e inset">红框=冲突</span>
    <span class="rb" style="box-shadow:0 0 0 2px #d4a72c inset">黄框=单源</span>
  </div>
  <div class="sub2">共 {n_events} 个事件 · 鼠标悬停任意事件看完整四个关键日</div>
  {grids}"""


# ==================== 合并站点(预警面板 + 日历,标签切换)====================
def _site_shell(meta, dash_body, cal_body, log_body):
    business_date = html.escape(str(meta.get("business_date") or ""))
    generated_utc = html.escape(str(meta.get("generated_at_utc") or ""))
    valid_until_utc = html.escape(str(meta.get("valid_until_utc") or ""))
    source_sha = html.escape(str(meta.get("source_sha") or "unknown"))
    short_sha = source_sha[:12]
    run_url = str(meta.get("run_url") or "")
    version_link = (f"<a href='{html.escape(run_url)}' target='_blank' rel='noopener'>{short_sha}</a>"
                    if run_url.startswith("https://github.com/") else short_sha)
    schema_version = html.escape(str(meta.get("schema_version") or "?"))
    css = """
    body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:0;background:#f6f8fa;color:#1f2328}
    .wrap{max-width:1180px;margin:0 auto;padding:24px}
    h1{font-size:22px;margin:0 0 4px}.sub{color:#656d76;font-size:13px;margin-bottom:16px}
    .sub2{color:#656d76;font-size:12px;margin:6px 0 16px}
    .snapshot{border-radius:8px;padding:9px 12px;margin:0 0 14px;font-size:13px}
    .snapshot.ok{background:#dafbe1;color:#116329;border:1px solid #82e596}
    .snapshot.bad{background:#ffebe9;color:#82071e;border:1px solid #ff8182;font-weight:600}
    /* 标签 */
    .tabs{display:flex;gap:8px;border-bottom:2px solid #e2e6ea;margin-bottom:20px}
    .tab{padding:9px 18px;font-size:14px;font-weight:600;color:#656d76;cursor:pointer;border:none;background:none;border-bottom:2px solid transparent;margin-bottom:-2px}
    .tab.active{color:#0969da;border-bottom-color:#0969da}
    .panel{display:none}.panel.active{display:block}
    /* 卡片 */
    .cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:22px}
    .card{flex:1;min-width:150px;background:#fff;border:1px solid #d0d7de;border-radius:10px;padding:14px}
    .card .n{font-size:26px;font-weight:700}.card .l{font-size:12px;color:#656d76}
    table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #d0d7de;border-radius:10px;overflow:hidden}
    th,td{padding:9px 11px;border-bottom:1px solid #eaeef2;text-align:left;font-size:13px;vertical-align:top}
    th{background:#f6f8fa;font-size:12px;color:#656d76}
    h2{font-size:17px;margin:28px 0 10px}h3{font-size:14px;margin:16px 0 6px}
    ul{margin:4px 0 0;padding-left:20px;font-size:13px}li{margin:3px 0}
    .legend{margin:4px 0 6px;font-size:12px;color:#444;line-height:2}
    .legend .c{display:inline-block;padding:1px 7px;border-radius:4px;margin:0 3px;font-weight:600}
    .legend .rb{display:inline-block;padding:1px 7px;border-radius:4px;margin:0 3px}
    .dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px;vertical-align:middle}
    .pgbtn{padding:4px 12px;border:1px solid #d0d7de;border-radius:6px;background:#fff;cursor:pointer;font-size:13px}
    .pgbtn:hover{background:#f3f4f6}
    /* 日历 */
    .month{margin-bottom:26px}.month h3{font-size:16px;margin:0 0 8px}
    table.cal{width:100%;border-collapse:collapse;background:#fff;border:1px solid #d0d7de;border-radius:10px;overflow:hidden;table-layout:fixed}
    table.cal th{background:#f6f8fa;color:#656d76;font-size:12px;padding:6px;border-bottom:1px solid #eaeef2}
    table.cal td{border:1px solid #eaeef2;vertical-align:top;height:118px;padding:5px;width:14.28%}
    td.empty{background:#fafbfc}
    td.today{background:#fffbe6;outline:2px solid #f0b429;outline-offset:-2px}
    .dn{font-size:12px;color:#8c959f;margin-bottom:4px;font-weight:600}
    .pill{border-radius:6px;margin-bottom:3px;cursor:default;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .pill.ex{font-size:12px;line-height:1.5;padding:3px 6px}
    .pill.ex .tk{font-weight:700}
    .pill.ex .ty{margin-left:4px}
    .pill.ex .val{margin-left:4px;font-weight:600}
    .pill.ex .kd{display:inline-block;background:rgba(0,0,0,.10);border-radius:3px;padding:0 4px;margin-left:5px;font-size:10px}
    .pill.sub{font-size:11px;padding:1px 6px;background:#f3f4f6;opacity:.92}
    .pill.sub .tk{font-weight:600}
    .pill.sub .sublbl{margin-left:4px;color:#6b7280}
    """
    js = """
    function showTab(t){
      document.querySelectorAll('.tab').forEach(function(e){e.classList.remove('active')});
      document.querySelectorAll('.panel').forEach(function(e){e.classList.remove('active')});
      document.getElementById('tab-'+t).classList.add('active');
      document.getElementById('panel-'+t).classList.add('active');
    }
    var secCur=1, secPer=30;
    function secRender(){
      var rows=document.querySelectorAll('#sec-table tr.secrow');
      var total=rows.length, pages=Math.max(1, Math.ceil(total/secPer));
      if(secCur<1)secCur=1; if(secCur>pages)secCur=pages;
      rows.forEach(function(r,i){ r.style.display=(i>=(secCur-1)*secPer && i<secCur*secPer)?'':'none'; });
      var info=document.getElementById('sec-pageinfo');
      if(info) info.textContent='第 '+secCur+' / '+pages+' 页(共 '+total+' 条)';
    }
    function secPage(d){ secCur+=d; secRender(); }
    function nyToday(){
      var parts=new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());
      var out={}; parts.forEach(function(p){out[p.type]=p.value});
      return new Date(out.year+'-'+out.month+'-'+out.day+'T00:00:00Z');
    }
    function crossedWeekdays(from,to){
      var n=0,d=new Date(from.getTime());
      while(d<to){d.setUTCDate(d.getUTCDate()+1);var w=d.getUTCDay();if(w!==0&&w!==6)n++;}
      return n;
    }
    (function(){
      var el=document.getElementById('snapshot-status');
      var snap=new Date(el.dataset.businessDate+'T00:00:00Z'), now=nyToday();
      var generated=new Date(el.dataset.generatedUtc), validUntil=new Date(el.dataset.validUntil), wallClock=new Date();
      var stale=isNaN(snap.getTime())||isNaN(generated.getTime())||isNaN(validUntil.getTime())||
        snap>now||crossedWeekdays(snap,now)>1||generated>new Date(wallClock.getTime()+10*60*1000)||
        validUntil<=generated||wallClock>validUntil;
      el.className='snapshot '+(stale?'bad':'ok');
      el.textContent=stale
        ? '⛔ 数据快照已过期或时间异常，请勿据此判断“无风险 / 无需操作”，并检查 GitHub Actions。'
        : '✅ 数据快照处于允许的美东业务日窗口。';
    })();
    // 支持用 ?tab=log 或 #log 直接打开某个标签页(供「最近更新」按钮跳转)
    (function(){
      var p = new URLSearchParams(location.search).get('tab') || (location.hash||'').replace('#','');
      if(p && document.getElementById('tab-'+p)) showTab(p);
    })();
    """
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>公司行动监控</title><style>{css}</style></head>
<body><div class="wrap">
  <h1>公司行动监控</h1>
  <div class="sub">更新 {html.escape(str(meta['generated']))} · 标的 {len(C.TICKERS)} 支 · 多源交叉核对(零容忍) · schema v{schema_version} · commit {version_link}</div>
  <div id="snapshot-status" class="snapshot" data-business-date="{business_date}" data-generated-utc="{generated_utc}" data-valid-until="{valid_until_utc}">正在核验快照时效…</div>
  <div class="tabs">
    <button class="tab active" id="tab-cal" onclick="showTab('cal')">📅 公司行动日历</button>
    <button class="tab" id="tab-dash" onclick="showTab('dash')">🔔 预警面板</button>
    <button class="tab" id="tab-log" onclick="showTab('log')">🆕 更新日志</button>
  </div>
  <div class="panel active" id="panel-cal">{cal_body}</div>
  <div class="panel" id="panel-dash">{dash_body}</div>
  <div class="panel" id="panel-log">{log_body}</div>
  <script>{js}</script>
</div></body></html>"""


def build_site(all_groups, source_health, alerts, meta):
    dash_body = build_dashboard(all_groups, source_health, alerts, meta)
    cal_body = build_calendar(all_groups, meta)
    log_body = build_changelog_panel(meta)
    return _site_shell(meta, dash_body, cal_body, log_body)
