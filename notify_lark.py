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


def _md_escape(s):
    return str(s).replace("[", "［").replace("]", "］")


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

    elements = [{
        "tag": "div",
        "text": {"tag": "lark_md",
                 "content": f"🆕 新发现 **{n_new}**　⏰ 临近 **{n_round}**　❗冲突 **{n_conf}**　🕳 空缺 **{n_gap}**"}
    }, {"tag": "hr"}]

    def section(title, lines):
        if not lines:
            return
        body = "\n".join(lines[:20])
        more = f"\n…… 等共 {len(lines)} 条" if len(lines) > 20 else ""
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                        "content": f"**{title}**\n{body}{more}"}})

    # 临近预警(最重要,放最前)
    rl = []
    for x in alerts["rounds"]:
        dates = f"除息 {x['date']}"
        if x.get("record"):
            dates += f" · 登记 {x['record']}"
        if x.get("pay"):
            dates += f" · 派发 {x['pay']}"
        val = ""
        if x.get("amount") is not None:
            val = f" ${x['amount']}"
        elif x.get("ratio"):
            val = f" {x['ratio']}"
        rl.append(f"• **{x['ticker']}** {ETYPE_CN.get(x['etype'], x['etype'])}{val} — "
                  f"<font color='red'>D-{x['days']}</font>({x['round']}天轮)　{dates}")
    section("⏰ 临近预警", rl)

    # 字段冲突
    cl = [f"• **{g.ticker}** {ETYPE_CN.get(g.etype, g.etype)} {g.anchor_date}:"
          f" {_md_escape('; '.join(g.conflicts))}" for g in alerts["conflicts"]]
    section("❗ 字段冲突(零容忍)", cl)

    # 数据空缺
    gl = [f"• **{g.ticker}** {ETYPE_CN.get(g.etype, g.etype)} {g.anchor_date}:"
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

    total = len(alerts["new"]) + len(alerts["rounds"]) + len(alerts["conflicts"]) + len(alerts["gaps"])
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
