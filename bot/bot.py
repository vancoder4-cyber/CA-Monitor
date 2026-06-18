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
import json
import requests
import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest, CreateMessageRequestBody,
    CreateImageRequest, CreateImageRequestBody, P2ImMessageReceiveV1,
)

import cards
from render import screenshot_calendar

APP_ID = os.environ["LARK_APP_ID"]
APP_SECRET = os.environ["LARK_APP_SECRET"]
SITE_URL = os.environ.get("SITE_URL", "https://vancoder4-cyber.github.io/CA-Monitor/").rstrip("/") + "/"
DATA_URL = SITE_URL + "data.json"

client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).domain(lark.LARK_DOMAIN).build()
_seen = set()  # message_id 去重


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


def send_calendar_image(chat_id, tab="cal"):
    path = screenshot_calendar(tab=tab)
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path, "rb") as f:
            req = CreateImageRequest.builder().request_body(
                CreateImageRequestBody.builder().image_type("message").image(f).build()).build()
        resp = client.im.v1.image.create(req)
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
        if cmd == "calendar":
            send_card(chat_id, cards.calendar_card(d, SITE_URL))
            send_calendar_image(chat_id, tab="cal")
        elif cmd == "alert":
            send_card(chat_id, cards.alert_card(d, SITE_URL))
    except Exception as e:
        print("on_message error:", e)


def main():
    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_message).build())
    cli = lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler, domain=lark.LARK_DOMAIN)
    print("CA-Monitor Lark bot 启动,等待 @ 指令……")
    cli.start()


if __name__ == "__main__":
    main()
