# -*- coding: utf-8 -*-
"""把预警推送到 Lark(飞书国际版)自定义机器人。

用自定义机器人 Webhook(无需建应用):群设置 → 机器人 → 添加自定义机器人 → 拿 Webhook URL,
可选开启「签名校验」拿到 secret。把 URL/secret 配到 .env:
    LARK_WEBHOOK=https://open.larksuite.com/open-apis/bot/v2/hook/xxxx
    LARK_SECRET=（开了签名校验才需要,否则留空)
    LARK_DASHBOARD_URL=（可选,卡片底部"打开面板"按钮指向的地址)
    LARK_NOTIFY_EMPTY=0      # 1=即使没有任何预警也推一条"全部正常"

发送交互卡片:临近预警 / 新发现 / 冲突 / 空缺,filing 带 SEC 原文链接。
"""
import os
import time
import json
import base64
import hashlib
import hmac
import datetime as dt

import requests

ETYPE_CN = {"dividend": "分红", "split": "拆股", "filing": "并购/公告"}


def _cfg():
    return {
        "webhook": os.environ.get("LARK_WEBHOOK", "").strip(),
        "secret": os.environ.get("LARK_SECRET", "").strip(),
        "dashboard": os.environ.get("LARK_DASHBOARD_URL", "").strip(),
        "notify_empty": os.environ.get("LARK_NOTIFY_EMPTY", "0").strip() == "1",
    }


def _sign(timestamp, secret):
    """Lark 签名:以 '{timestamp}\\n{secret}' 为 HMAC-SHA256 的 key,消息体为空,base64。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _pick(src_fields, field):
    return next((v.get(field) for v in src_fields.values() if v.get(field)), None)


def _sec_url(g):
    return (g.by_source.get("SEC") or {}).get("url", "")


def _load_mentions():
    """从 refs.json 读 alert_mention_open_ids:催办推送要 @ 的 open_id 列表('all'=@所有人)。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "refs.json")
    try:
        ids = json.load(open(path, encoding="utf-8")).get("alert_mention_open_ids") or []
        return [str(x).strip() for x in ids if str(x).strip()]
    except Exception:
        return []


def _at_tags(open_ids):
    """生成 Lark 卡片 @ 标签:open_id → <at id=ou_xxx></at>;'all' → @所有人。"""
    return "".join(f"<at id={oid}></at>" for oid in open_ids)


def _nasdaq_div(ticker):
    return f"https://www.nasdaq.com/market-activity/stocks/{ticker.lower()}/dividend-history"


def _refs(ticker, etype, g=None, decl_url=None, ir_url=None):
    """核对链接:指向『对应那一条公司行动』本身,而非整列表。
       filing → 该 filing 的 SEC 原文文件;
       dividend → 宣告 8-K(精确匹配)> 公司 IR 分红页(refs.json)> Nasdaq 分红记录。"""
    if g is not None:
        u = _sec_url(g)
        if u:
            return f"\n　📄 [SEC原文(本事件)]({u})"
        decl_url = decl_url or getattr(g, "decl_url", "")
        ir_url = ir_url or getattr(g, "ir_url", "")
    if etype == "dividend":
        if decl_url:
            return f"\n　📄 [宣告 8-K(本次分红)]({decl_url})"
        if ir_url:
            return f"\n　🏛 [公司IR 分红页]({ir_url})"
        return f"\n　🔗 [Nasdaq 分红记录]({_nasdaq_div(ticker)})"
    return ""


def _md_escape(s):
    return str(s).replace("[", "［").replace("]", "］")


def _dates(x):
    """关键日链:宣告 · 登记 · 除息/生效 · 派发(有哪个显示哪个,与查代码口径一致)。"""
    lab = "除息" if x.get("etype") == "dividend" else "生效"
    parts = []
    if x.get("decl"):
        parts.append(f"宣告 {x['decl']}")
    if x.get("record"):
        parts.append(f"登记 {x['record']}")
    if x.get("date"):
        parts.append(f"{lab} {x['date']}")
    if x.get("pay"):
        parts.append(f"派发 {x['pay']}")
    return " · ".join(parts)



def _val(x):
    """金额/比例门禁:有未确认冲突 → 不给确定值,标『待人工确认·勿据此执行』。
    人工「确认」后冲突消解,才会恢复显示确定值。"""
    if not x.get("acked") and not x.get("disputed") and (x.get("amt_srcs") or 0) == 1 and (
            x.get("amount") is not None or x.get("ratio")):
        v = x.get("amount") if x.get("amount") is not None else x.get("ratio")
        return f" <font color='orange'>⚠️单源未交叉验证({v})· 待人工确认,勿据此执行</font>"
    if x.get("disputed") and not x.get("acked"):
        vals = x.get("dispute_vals") or {}
        pairs = " / ".join(f"{v}" for v in dict.fromkeys(vals.values()))
        return f" <font color='red'>⚠️各源不一致({pairs})· 待人工确认,勿据此执行</font>"
    if x.get("amount") is not None:
        return f" ${x['amount']}"
    if x.get("ratio"):
        return f" {x['ratio']}"
    return ""

def _build_card(alerts, meta, dashboard_url=""):
    n_new = len(alerts["new"]); n_round = len(alerts["rounds"])
    n_conf = len(alerts["conflicts"]); n_gap = len(alerts["gaps"])
    # 有冲突/空缺 → 红;有临近/新发现 → 蓝;否则绿
    if n_conf or n_gap:
        template = "red"
    elif n_round or n_new:
        template = "blue"
    else:
        template = "green"

    n_pending = len(alerts.get("pending", []))
    n_ann = len(alerts.get("announced", []))
    elements = [{
        "tag": "div",
        "text": {"tag": "lark_md",
                 "content": f"📣 新公告 **{n_ann}**　⏳ 待执行 **{n_pending}**　🆕 新发现 **{n_new}**"
                            f"　❗冲突 **{n_conf}**　🕳 空缺 **{n_gap}**"}
    }, {"tag": "hr"}]

    def section(title, lines):
        if not lines:
            return
        body = "\n".join(lines[:30])
        more = f"\n…… 等共 {len(lines)} 条" if len(lines) > 30 else ""
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                        "content": f"**{title}**\n{body}{more}"}})

    # 🙋 待人工确认(不豁免:挂着就一直报,只有「确认」能消解)—— 超期则 @ 人升级
    _rv = alerts.get("review") or {}
    _esc = _rv.get("escalate_days", 3)
    if _rv.get("open"):
        _m = _load_mentions()
        head = (f"🙋 **待人工确认 {_rv['open']} 条**"
                f"(冲突 {_rv.get('conflicts',0)} · 空缺 {_rv.get('gaps',0)} · 未宣告预估 {_rv.get('unconfirmed',0)})"
                f"　最久已挂 **{_rv.get('max_age',0)} 天**")
        if _rv.get("overdue") and _m:
            head = (_at_tags(_m) + f" ❗ 有 **{_rv['overdue']}** 条异常超过 {_esc} 天没人确认,请尽快处理\n" + head)
        head += "\n　👉 核对后在群里发 **确认 代码 [正确值]**(例:`确认 TSM 1.11362`)即可消解;不确认会一直报。"
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": head}})
        elements.append({"tag": "hr"})

    # ⏰ 临近预警(运营催办)
    rl = []
    for x in alerts["rounds"]:
        dates = _dates(x)
        val = _val(x)
        prod = ("[" + "+".join(x["products"]) + "] ") if x.get("products") else ""
        line = (f"• {prod}**{x['ticker']}** {ETYPE_CN.get(x['etype'], x['etype'])}{val} — "
                f"<font color='red'>D-{x['days']}</font>({x['round']}天轮)　{dates}")
        if x.get("ops"):
            line += f"\n　👉 {x['ops']}"
        if x.get("risk_copy"):
            line += f"\n　🛡 {x['risk_copy']}"
        rl.append(line)
    # 轮询预警 @:有催办事项且配置了名单时,在催办区顶部 @ 对应的人
    _mentions = _load_mentions()
    if rl and _mentions:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                        "content": _at_tags(_mentions) + " ⏰ 有临近催办事项,请及时处理"}})
    section("⏰ 临近预警(运营催办)", rl)

    # 优先级互斥:已在催办里出现的事件,后面的区不再重复
    def _sig(x):
        return (x.get("ticker"), x.get("etype"), x.get("date"))
    claimed = {_sig(x) for x in alerts.get("rounds", [])}

    # 📣 新公告:刚扫到 declaration date 的事件(跳过已在催办的)
    al = []
    for x in alerts.get("announced", []):
        if _sig(x) in claimed:
            continue
        claimed.add(_sig(x))
        prod = ("[" + "+".join(x["products"]) + "] ") if x.get("products") else ""
        val = _val(x)
        days = f" · <font color='red'>还剩 {x['days']} 天</font>" if x.get("days") is not None else ""
        al.append(f"• {prod}**{x['ticker']}** {ETYPE_CN.get(x['etype'], x['etype'])}{val} —— "
                  f"宣告 {x.get('decl')} · 除息 {x['date']}{days}")
    section("📣 新公告(刚宣告)", al)

    # ⏳ 待执行(已公告未发生)—— 跳过已在催办/新公告里出现的
    pl = []
    for x in alerts.get("pending", []):
        if _sig(x) in claimed:
            continue
        prod = ("[" + "+".join(x["products"]) + "] ") if x.get("products") else ""
        val = _val(x)
        dates = _dates(x)
        line = (f"• {prod}**{x['ticker']}** {ETYPE_CN.get(x['etype'], x['etype'])}{val} — "
                f"<font color='red'>还剩 {x['days']} 天</font>　{dates}")
        if not x.get("confirmed", True):
            line += (f"\n　⚠️ <font color='orange'>未见宣告日,仅 {'/'.join(x.get('srcs') or [])} 单源"
                     f"(可能是预估,公司尚未正式公告)—— 请勿据此执行</font>")
        for rn in x.get("risk", []):
            line += f"\n　⚠️ {rn}"
        line += _refs(x["ticker"], x["etype"], decl_url=x.get("decl_url"), ir_url=x.get("ir_url"))
        pl.append(line)
    section("⏳ 待执行(已公告未发生)", pl)

    # 字段冲突(零容忍:不豁免,每次都报,直到人工「确认」)
    def _aged(g):
        a = getattr(g, "age_days", 0) or 0
        if a >= _esc:
            return f"　<font color='red'>⏳已挂 {a} 天未确认</font>"
        return f"　<font color='grey'>已挂 {a} 天</font>" if a else ""
    def _adr(g):  # ADR 预扣税提示:保证认税前毛额
        try:
            import reconcile as _R
            n = _R.adr_tax_note(g.ticker, g.by_source) if g.etype == "dividend" else ""
        except Exception:
            n = ""
        return f"\n　<font color='red'>{n}</font>" if n else ""
    cl = [f"• **{g.ticker}** {ETYPE_CN.get(g.etype, g.etype)} {g.anchor_date}:"
          f" {_md_escape('; '.join(g.conflicts))}{_aged(g)}{_adr(g)}{_refs(g.ticker, g.etype, g)}"
          for g in alerts["conflicts"]]
    section("❗ 字段冲突(零容忍 · 需人工确认)", cl)

    # 数据空缺(同样需人工确认)
    gl = [f"• **{g.ticker}** {ETYPE_CN.get(g.etype, g.etype)} {g.anchor_date}{_aged(g)}:"
          f" {_md_escape('; '.join(g.gaps))}" for g in alerts["gaps"]]
    section("🕳 数据空缺", gl)

    # 新发现
    nl = []
    for g in alerts["new"]:
        amt = _pick(g.by_source, "amount"); ratio = _pick(g.by_source, "ratio")
        val = (f" ${amt}" if amt is not None else "") + (f" {ratio}" if ratio else "")
        line = f"• **{g.ticker}** {ETYPE_CN.get(g.etype, g.etype)} {g.anchor_date}{val}"
        if g.etype == "filing":
            line += f" {_md_escape(g.note or '')}"
            u = _sec_url(g)
            if u:
                line += f" [SEC原文]({u})"
        nl.append(line)
    section("🆕 新发现事件", nl)

    if dashboard_url:
        elements.append({"tag": "hr"})
        elements.append({"tag": "action", "actions": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "打开公司行动面板"},
            "url": dashboard_url, "type": "primary"}]})

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": template,
                       "title": {"tag": "plain_text",
                                 "content": f"📣 公司行动预警 · {meta['generated']}"}},
            "elements": elements,
        }
    }


def notify(alerts, meta):
    """根据 .env 配置推送到 Lark。返回 (sent: bool, info: str)。"""
    cfg = _cfg()
    if not cfg["webhook"]:
        return False, "未配置 LARK_WEBHOOK,跳过推送"

    total = (len(alerts["new"]) + len(alerts["rounds"]) + len(alerts["conflicts"])
             + len(alerts["gaps"]) + len(alerts.get("pending", [])) + len(alerts.get("announced", [])))
    if total == 0 and not cfg["notify_empty"]:
        return False, "无预警内容,跳过(设 LARK_NOTIFY_EMPTY=1 可强制推送)"

    payload = _build_card(alerts, meta, cfg["dashboard"])
    if cfg["secret"]:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = _sign(ts, cfg["secret"])

    try:
        r = requests.post(cfg["webhook"], json=payload, timeout=15)
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        code = j.get("code", j.get("StatusCode", -1))
        if r.status_code == 200 and code in (0, None):
            return True, f"已推送 {total} 条预警到 Lark"
        return False, f"Lark 返回异常: HTTP {r.status_code} {r.text[:160]}"
    except Exception as e:
        return False, f"推送失败: {e}"


if __name__ == "__main__":
    # 自检:发一条测试卡片
    fake = {"new": [], "rounds": [], "conflicts": [], "gaps": []}
    meta = {"generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
    os.environ.setdefault("LARK_NOTIFY_EMPTY", "1")
    print(notify(fake, meta))
