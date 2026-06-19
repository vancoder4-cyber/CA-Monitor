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
import subprocess
import requests
import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest, CreateMessageRequestBody,
    CreateImageRequest, CreateImageRequestBody, P2ImMessageReceiveV1,
)

import cards

APP_ID = os.environ["LARK_APP_ID"]
APP_SECRET = os.environ["LARK_APP_SECRET"]
SITE_URL = os.environ.get("SITE_URL", "https://vancoder4-cyber.github.io/CA-Monitor/").rstrip("/") + "/"
DATA_URL = SITE_URL + "data.json"
HERE = os.path.dirname(os.path.abspath(__file__))

client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).domain(lark.LARK_DOMAIN).build()
_seen = set()      # message_id 去重
BOT_OPEN_ID = None  # 机器人自身 open_id(用于判断是否被 @)


def get_bot_open_id():
    """取机器人自身 open_id,用于在群里只回应被 @ 的消息。"""
    try:
        t = requests.post(
            "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=15
        ).json().get("tenant_access_token")
        r = requests.get("https://open.larksuite.com/open-apis/bot/v3/info",
                         headers={"Authorization": f"Bearer {t}"}, timeout=15).json()
        return (r.get("bot") or {}).get("open_id")
    except Exception as e:
        print("get_bot_open_id err:", e)
        return None


def fetch_data():
    try:
        r = requests.get(DATA_URL, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print("fetch data.json err:", e)
    return {}


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
    t = re.sub(r"@_user_\d+|@_all", "", text or "").strip().lower()
    if any(k in t for k in ("关于", "介绍", "about")):
        return "about"
    if any(k in t for k in ("风险", "风控", "risk")):
        return "risk"
    if any(k in t for k in ("新公告", "公告", "announce")):
        return "announce"
    if any(k in t for k in ("覆盖", "资产", "标的", "coverage")):
        return "coverage"
    if any(k in t for k in ("今日", "今天", "today")):
        return "today"
    if any(k in t for k in ("本周", "week")):
        return "week"
    if any(k in t for k in ("日历", "calendar", "cal")):
        return "calendar"
    if any(k in t for k in ("预警", "面板", "alert", "dashboard")):
        return "alert"
    if any(k in t for k in ("帮助", "help", "?", "？")):
        return "help"
    return "help"


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
        print(f"[msg] chat={chat_id} text={text!r} -> {cmd}")

        if cmd == "help":
            send_text(chat_id, cards.HELP_TEXT.replace("**", ""))
            return

        d = fetch_data()
        if cmd == "about":
            send_card(chat_id, cards.about_card(d, SITE_URL))
        elif cmd == "risk":
            send_card(chat_id, cards.risk_card(d, SITE_URL))
        elif cmd == "today":
            send_card(chat_id, cards.today_card(d, SITE_URL))
        elif cmd == "week":
            send_card(chat_id, cards.week_card(d, SITE_URL))
        elif cmd == "announce":
            send_card(chat_id, cards.announce_card(d, SITE_URL))
        elif cmd == "coverage":
            send_card(chat_id, cards.coverage_card(d, SITE_URL))
        elif cmd == "calendar":
            send_card(chat_id, cards.calendar_card(d, SITE_URL))
            send_calendar_image(chat_id, d)
        elif cmd == "alert":
            send_card(chat_id, cards.alert_card(d, SITE_URL))
    except Exception as e:
        print("on_message error:", e)


def main():
    global BOT_OPEN_ID
    BOT_OPEN_ID = get_bot_open_id()
    print("bot open_id:", BOT_OPEN_ID)
    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_message).build())
    cli = lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler, domain=lark.LARK_DOMAIN)
    print("CA-Monitor Lark bot 启动,只回应 @ 机器人 的指令……")
    cli.start()


if __name__ == "__main__":
    main()
