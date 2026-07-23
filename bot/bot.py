# -*- coding: utf-8 -*-
"""交互式 Lark 机器人:@机器人 + 指令 → 回卡片 / 日历截图。

长连接(WebSocket)模式,无需公网 IP/回调地址,适合跑在 PaaS。
指令:日历 / 预警(面板) / 帮助。

环境变量:
    LARK_APP_ID, LARK_APP_SECRET   —— Lark 自定义应用凭证
    SITE_URL                        —— GitHub Pages 站点(默认 CA-Monitor)
"""
import os
import re
import sys
import json
import time
import threading
import subprocess
import requests
import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest, CreateMessageRequestBody,
    CreateImageRequest, CreateImageRequestBody, P2ImMessageReceiveV1,
)

import cards
import ack

APP_ID = os.environ["LARK_APP_ID"]
APP_SECRET = os.environ["LARK_APP_SECRET"]
SITE_URL = os.environ.get("SITE_URL", "https://vancoder4-cyber.github.io/CA-Monitor/").rstrip("/") + "/"
DATA_URL = SITE_URL + "data.json"
HERE = os.path.dirname(os.path.abspath(__file__))

client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).domain(lark.LARK_DOMAIN).build()
_seen = set()      # message_id 去重
BOT_OPEN_ID = None  # 机器人自身 open_id(用于判断是否被 @)


def _tenant_token():
    try:
        return requests.post(
            "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=15
        ).json().get("tenant_access_token")
    except Exception as e:
        print("tenant token err:", e)
        return None


def get_bot_open_id():
    """取机器人自身 open_id,用于在群里只回应被 @ 的消息。"""
    try:
        t = _tenant_token()
        r = requests.get("https://open.larksuite.com/open-apis/bot/v3/info",
                         headers={"Authorization": f"Bearer {t}"}, timeout=15).json()
        return (r.get("bot") or {}).get("open_id")
    except Exception as e:
        print("get_bot_open_id err:", e)
        return None


_NAME_CACHE = {}


def get_user_name(open_id):
    """open_id → 显示名(留痕『谁确认的』要可读)。需通讯录 contact 读权限;
    没权限/取不到时返回空串,ack 里仍留 open_id 兜底,不影响确认。"""
    if not open_id:
        return ""
    if open_id in _NAME_CACHE:
        return _NAME_CACHE[open_id]
    name = ""
    try:
        t = _tenant_token()
        r = requests.get(
            f"https://open.larksuite.com/open-apis/contact/v3/users/{open_id}",
            params={"user_id_type": "open_id"},
            headers={"Authorization": f"Bearer {t}"}, timeout=15).json()
        name = ((r.get("data") or {}).get("user") or {}).get("name", "") or ""
    except Exception as e:
        print("get_user_name err:", e)
    _NAME_CACHE[open_id] = name
    return name


def fetch_data():
    try:
        r = requests.get(DATA_URL, timeout=15)
        if r.status_code == 200:
            return apply_acks(r.json())
    except Exception as e:
        print("fetch data.json err:", e)
    return {}


def apply_acks(d):
    """把最新的人工确认叠加到(可能过期的)data.json 上,让卡片**即时**反映确认结果:
    已确认的『字段冲突 / 数据空缺』当场从风险/总览里剔除,不必等流水线(3×/日)重跑。
    读的是仓库里实时的 acknowledged.json —— 你一发『确认』,下一次 @bot 就看不到那条了。"""
    if not isinstance(d, dict):
        return d
    try:
        acks = ack.get_acks()
    except Exception as e:
        print("apply_acks get_acks err:", e)
        acks = []
    if not acks:
        return d
    ackset = {(a.get("ticker"), a.get("date")) for a in acks}
    # 冲突/空缺:已确认 → 直接剔除(风险卡的『数据冲突』、总览的『空缺』当场消失)
    for key in ("conflicts", "gaps"):
        lst = d.get(key)
        if isinstance(lst, list):
            d[key] = [g for g in lst if (g.get("ticker"), g.get("date")) not in ackset]
    # 待执行/日历/新公告:标记已确认(解除金额门禁显示),但保留事件本身(真实公司行动不因确认而消失)
    for key in ("pending", "calendar", "announced"):
        for x in d.get(key, []) or []:
            if (x.get("ticker"), x.get("date")) in ackset:
                x["acked"] = True
    c = d.get("counts")
    if isinstance(c, dict):
        if isinstance(d.get("conflicts"), list):
            c["conflicts"] = len(d["conflicts"])
        if isinstance(d.get("gaps"), list):
            c["gaps"] = len(d["gaps"])
    return d


def _send(chat_id, msg_type, content):
    req = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(
        CreateMessageRequestBody.builder().receive_id(chat_id)
        .msg_type(msg_type).content(content).build()).build()
    resp = client.im.v1.message.create(req)
    if not resp.success():
        print("send fail:", resp.code, resp.msg)


def send_card(chat_id, card):
    _send(chat_id, "interactive", json.dumps(card, ensure_ascii=False))


def send_text(chat_id, text):
    _send(chat_id, "text", json.dumps({"text": text}, ensure_ascii=False))


def send_calendar_image(chat_id, data):
    # 用 Pillow 直接画当月月历(不再网页截图)
    path = "/tmp/calendar.png"
    try:
        if os.path.exists(path):
            os.remove(path)
        from render import draw_month
        draw_month(data.get("calendar", []), path)
    except Exception as e:
        print("draw calendar err:", e)
    if not os.path.exists(path):
        return False
    try:
        with open(path, "rb") as f:
            req = CreateImageRequest.builder().request_body(
                CreateImageRequestBody.builder().image_type("message").image(f).build()).build()
            resp = client.im.v1.image.create(req)   # 必须在 with 内,否则文件已关闭
        if not resp.success():
            print("image upload fail:", resp.code, resp.msg)
            return False
        key = resp.data.image_key
        _send(chat_id, "image", json.dumps({"image_key": key}))
        return True
    except Exception as e:
        print("send image err:", e)
        return False


def parse_command(text):
    # 指令唯一来源在 cards.COMMANDS
    return cards.parse_command(text)


def on_message(data: P2ImMessageReceiveV1):
    try:
        msg = data.event.message
        mid = msg.message_id
        if mid in _seen:
            return
        _seen.add(mid)
        if len(_seen) > 500:
            _seen.clear()
        chat_id = msg.chat_id
        chat_type = getattr(msg, "chat_type", "") or ""

        # 打印发送人 open_id(用于维护 @ 联系人表:让对方 @ 一次机器人,从日志取 open_id)
        sender_oid = None
        try:
            sender_oid = data.event.sender.sender_id.open_id
        except Exception:
            pass
        print(f"[sender] chat={chat_id} open_id={sender_oid}")

        # 群聊里:只在被 @ 机器人时才响应(私聊则照常)
        mentioned = False
        mentions = getattr(msg, "mentions", None) or []
        for m in mentions:
            oid = getattr(getattr(m, "id", None), "open_id", None)
            if BOT_OPEN_ID and oid == BOT_OPEN_ID:
                mentioned = True
        if chat_type == "group":
            if BOT_OPEN_ID and not mentioned:
                return
            if not BOT_OPEN_ID and not mentions:  # 兜底:拿不到 open_id 时,至少要求有 @
                return

        text = ""
        try:
            text = json.loads(msg.content or "{}").get("text", "")
        except Exception:
            pass
        cmd = parse_command(text)
        d = fetch_data()
        ticker = cards.find_ticker(text, d)
        # 查代码:显式『查』指令,或直接发了一个已覆盖的代码(未命中其它指令时)
        if cmd == "confirm":
            clean = re.sub(r"@_user_\d+|@_all", "", text or "")  # 去掉 @ 占位符再取数值,避免误读
            # 先摘出日期(YYYY-MM-DD)再取数值 —— 否则「2026」会被当成金额。
            # 同一标的可能有多条不同值的异常(如 KLAC 2.3 / 1.9),必须能指定是哪一条。
            mdate = re.search(r"\d{4}-\d{2}-\d{2}", clean)
            date = mdate.group(0) if mdate else None
            rest = clean.replace(date, "") if date else clean
            mval = re.search(r"\d+(?:\.\d+)?", rest)
            value = mval.group(0) if mval else None

            # 备注:去掉指令词/代码/值之后剩下的自由文字(如「已比对公司 8-K」)
            note = rest
            if value:
                note = note.replace(value, "", 1)
            for _kw in ("确认", "confirm", "已核对"):
                note = re.sub(_kw, "", note, flags=re.I)
            if ticker:
                note = re.sub(rf"\b{re.escape(ticker)}\b", "", note, flags=re.I)
            note = note.strip(" :：,，、-—\t")

            etype = None
            if date:
                # 指定了日期:在冲突/待执行/日历里定位该标的该日期的事件
                for key in ("conflicts", "pending", "calendar", "gaps"):
                    for c in d.get(key, []) or []:
                        if c.get("ticker") == ticker and c.get("date") == date:
                            etype = c.get("etype")
                            break
                    if etype:
                        break
            else:
                # 没给日期:默认取该标的的第一条冲突(多条不同值时,建议带上日期)
                for c in d.get("conflicts", []) or []:
                    if c.get("ticker") == ticker:
                        etype, date = c.get("etype"), c.get("date")
                        break
            print(f"[msg] chat={chat_id} text={text!r} -> confirm {ticker} {value} @{date}")
            if not ticker:
                send_card(chat_id, cards.confirm_card(
                    False, "没认出代码。用法:`确认 代码 [正确值] [日期] [备注]`,例:`确认 KLAC 2.3 2026-05-18 已比对公司8-K`",
                    site_url=SITE_URL))
                return
            # ADR 防呆:若确认的值像「净额(税后)」——低于该事件毛额约 5% 以上——就警告(仍记录)
            warn = ""
            _ag = None
            for c in (d.get("conflicts", []) or []):
                if c.get("ticker") == ticker and c.get("date") == date:
                    _ag = c.get("adr_gross")
                    break
            try:
                if _ag and value is not None and float(value) < float(_ag) * 0.95:
                    warn = (f"⚠️ 你确认的 **{value}** 像是**净额(税后)**;该 ADR **毛额(税前)约 {_ag}**。"
                            f"我们认毛额 —— 若填错请用毛额重发一次(会覆盖)。")
            except (TypeError, ValueError):
                pass
            by_name = get_user_name(sender_oid)
            ok, msg = ack.add_ack(ticker, value, etype, date,
                                  by=sender_oid or "", by_name=by_name, note=note)
            send_card(chat_id, cards.confirm_card(ok, msg, ticker, value, SITE_URL, date, etype, warn))
            return
        if cmd == "audit":
            # 留痕库:拉最近确认记录(可只看某个标的)。经 GH API 读 data/ack_log.json
            log = ack.get_ack_log(limit=200)
            if ticker:
                log = [e for e in log if e.get("ticker") == ticker]
            print(f"[msg] chat={chat_id} -> audit ticker={ticker} n={len(log)}")
            send_card(chat_id, cards.audit_card(log[:15], SITE_URL, ticker))
            return
        if cmd == "request":
            req = re.sub(r"@_user_\d+|@_all", "", text or "").strip()
            for kw in ("需求提报", "需求", "提报", "反馈", "建议", "feature", "feedback"):
                if req.lower().startswith(kw.lower()):
                    req = req[len(kw):].strip(" :：")
                    break
            print(f"[msg] chat={chat_id} -> request {req!r} by={sender_oid}")
            if not req:
                send_card(chat_id, cards.request_card(False, "", "", SITE_URL))
                return
            ok, msg = ack.add_request(req, by=sender_oid or "")
            send_card(chat_id, cards.request_card(ok, msg, req, SITE_URL))
            return
        if cmd == "lookup" or (ticker and cmd == "help"):
            print(f"[msg] chat={chat_id} text={text!r} -> lookup {ticker}")
            send_card(chat_id, cards.lookup_card(d, ticker, SITE_URL))
            return
        print(f"[msg] chat={chat_id} text={text!r} -> {cmd}")

        if cmd == "help":
            send_text(chat_id, cards.HELP_TEXT.replace("**", ""))
            return

        if cmd == "about":
            send_card(chat_id, cards.about_card(d, SITE_URL))
        elif cmd == "risk":
            send_card(chat_id, cards.risk_card(d, SITE_URL))
        elif cmd == "today":
            send_card(chat_id, cards.today_card(d, SITE_URL))
        elif cmd == "week":
            send_card(chat_id, cards.week_card(d, SITE_URL))
        elif cmd == "upcoming":
            send_card(chat_id, cards.upcoming_card(d, SITE_URL))
        elif cmd == "announce":
            send_card(chat_id, cards.announce_card(d, SITE_URL))
        elif cmd == "coverage":
            send_card(chat_id, cards.coverage_card(d, SITE_URL))
        elif cmd == "changelog":
            send_card(chat_id, cards.changelog_card(d, SITE_URL))
        elif cmd == "calendar":
            send_card(chat_id, cards.calendar_card(d, SITE_URL))
            send_calendar_image(chat_id, d)
    except Exception as e:
        print("on_message error:", e)


def _heartbeat_loop():
    """掉线告警:每 5 分钟 ping 一次 HEARTBEAT_URL(如 healthchecks.io 的 check URL)。
    bot 一旦挂了/长连接断了/进程停了,就不再 ping,监控方超时后发邮件/Slack 告警。
    未配置 HEARTBEAT_URL 则不启用(静默跳过)。"""
    url = os.environ.get("HEARTBEAT_URL", "").strip()
    if not url:
        print("heartbeat: 未配置 HEARTBEAT_URL,跳过(掉线告警未启用)")
        return
    print("heartbeat: 已启用,每 5 分钟上报一次")
    while True:
        try:
            requests.get(url, timeout=10)
        except Exception as e:
            print("heartbeat err:", e)
        time.sleep(300)


def main():
    global BOT_OPEN_ID
    BOT_OPEN_ID = get_bot_open_id()
    print("bot open_id:", BOT_OPEN_ID)
    threading.Thread(target=_heartbeat_loop, daemon=True).start()  # 掉线告警心跳
    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_message).build())
    cli = lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler, domain=lark.LARK_DOMAIN)
    print("CA-Monitor Lark bot 启动,只回应 @ 机器人 的指令……")
    cli.start()


if __name__ == "__main__":
    main()
