# -*- coding: utf-8 -*-
"""交互式 Lark 机器人:@机器人 + 指令 → 回卡片 / 日历截图。

长连接(WebSocket)模式,无需公网 IP/回调地址,适合跑在 PaaS。
指令:日历 / 预警(面板) / 帮助。

环境变量:
    LARK_APP_ID, LARK_APP_SECRET   —— Lark 自定义应用凭证
    SITE_URL                        —— GitHub Pages 站点(默认 CA-Monitor)
    LARK_WRITE_ALLOWED_OPEN_IDS     —— 可执行写操作的操作员 open_id 白名单
"""
import os
import re
import sys
import json
import time
import datetime as dt
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
WRITE_COMMANDS = {"filing_resolve", "forecast", "confirm", "request"}


def write_authorized(open_id):
    """生产写操作必须命中 Railway Secret 中的操作员白名单。"""
    allowed = {
        value.strip()
        for value in os.environ.get("LARK_WRITE_ALLOWED_OPEN_IDS", "").split(",")
        if value.strip()
    }
    return bool(open_id and open_id in allowed)


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
        if r.status_code != 200:
            return {"_snapshot_error": f"Pages data.json HTTP {r.status_code}"}
        try:
            payload = r.json()
        except Exception as e:
            return {"_snapshot_error": f"Pages data.json 不是有效 JSON：{e}"}
        problem = cards.validate_snapshot(payload)
        if problem:
            return {"_snapshot_error": problem}
        payload["_bot_build_sha"] = (
            os.environ.get("RAILWAY_GIT_COMMIT_SHA")
            or os.environ.get("GIT_COMMIT_SHA")
            or os.environ.get("SOURCE_VERSION")
            or "unknown"
        )
        return apply_forecasts(apply_acks(payload))
    except Exception as e:
        print("fetch data.json err:", e)
        return {"_snapshot_error": f"Pages data.json 请求失败：{e}"}


def apply_acks(d):
    """把最新人工确认叠加到可能过期的 data.json。

    已精确确认的字段冲突/数据空缺会即时从异常列表剔除；3% 产品动作结论仍由
    下一轮流水线结合币种、证券单位和参考价统一重算，Bot 不在旧快照上武断放行。
    """
    if not isinstance(d, dict):
        return d
    try:
        acks = ack.get_acks()
    except Exception as e:
        print("apply_acks get_acks err:", e)
        acks = []
    if not acks:
        return d
    # 生效键必须包含事件类型；同日可能同时发生现金分红和拆股。
    ackset = {(a.get("ticker"), a.get("etype"), a.get("date")) for a in acks
              if a.get("etype") and a.get("date")}
    # 冲突/空缺:已确认 → 直接剔除(风险卡的『数据冲突』、总览的『空缺』当场消失)
    for key in ("conflicts", "gaps"):
        lst = d.get(key)
        if isinstance(lst, list):
            d[key] = [g for g in lst
                      if (g.get("ticker"), g.get("etype"), g.get("date")) not in ackset]
    # 待执行/日历/新公告只叠加确认标记；数值门禁和产品动作等待流水线统一重算。
    for key in ("pending", "calendar", "announced", "recent_declares"):
        for x in d.get(key, []) or []:
            if (x.get("ticker"), x.get("etype"), x.get("date")) in ackset:
                x["acked"] = True
    c = d.get("counts")
    if isinstance(c, dict):
        if isinstance(d.get("conflicts"), list):
            c["conflicts"] = len(d["conflicts"])
        if isinstance(d.get("gaps"), list):
            c["gaps"] = len(d["gaps"])
    return d


def apply_forecasts(d):
    """将刚通过机器人写入的「观察」即时叠加到网页快照。

    Pages 下次流水线前，避免旧 data.json 继续把单源预测展示为执行催办。
    """
    if not isinstance(d, dict):
        return d
    try:
        watches = ack.get_forecasts()
    except Exception as e:
        print("apply_forecasts get_forecasts err:", e)
        watches = []
    business_date = cards._business_date(d)
    watchmap = {}
    for w in watches:
        if not isinstance(w, dict) or w.get("status", "watching") != "watching":
            continue
        ticker, etype, date = w.get("ticker"), w.get("etype"), w.get("date")
        if not (isinstance(ticker, str) and isinstance(etype, str)
                and isinstance(date, str) and cards.is_monitored_ticker(d, ticker)):
            continue
        try:
            # forecast_watch.json 是只追加的历史记录；预计日已过的观察由流水线
            # 负责发「失效」通知，Bot 叠加层不得把它重新显示成活动预测。
            if dt.date.fromisoformat(date) < business_date:
                continue
        except ValueError:
            continue
        watchmap[(ticker, etype, date)] = w
    watchset = set(watchmap)
    if not watchset:
        return d
    forecasts = list(d.get("forecasts") or [])
    pending = []
    for x in d.get("pending", []) or []:
        key = (x.get("ticker"), x.get("etype"), x.get("date"))
        if key in watchset and not x.get("confirmed", False):
            x["watching"] = True
            x.setdefault("watch_note", "已人工标记观察，等待公司宣告")
            forecasts.append(x)
        else:
            pending.append(x)
    for x in forecasts:
        if (x.get("ticker"), x.get("etype"), x.get("date")) in watchset:
            x["watching"] = True
    published = forecasts + pending
    for field in ("calendar", "announced", "recent_declares"):
        published.extend(d.get(field, []) or [])
    existing = {(x.get("ticker"), x.get("etype"), x.get("date")) for x in published}
    coverage = {x.get("ticker"): x for x in d.get("coverage", [])}
    for key, watch in watchmap.items():
        # 正式化后的事件仍可能保留 watching=True（用于发「预测转正式」状态
        # 更新）；日期小幅改动时也不能把旧预计日再次合成为第二条预测。
        ticker, etype, date = key
        shifted_match = False
        for event in published:
            if not (event.get("watching") and event.get("ticker") == ticker
                    and event.get("etype") == etype and event.get("date")):
                continue
            try:
                shifted_match = abs(
                    (dt.date.fromisoformat(event["date"]) - dt.date.fromisoformat(date)).days
                ) <= 14
            except (TypeError, ValueError):
                shifted_match = False
            if shifted_match:
                break
        if key in existing or shifted_match:
            continue
        try:
            days = (dt.date.fromisoformat(date) - business_date).days
        except (TypeError, ValueError):
            continue
        cov = coverage.get(ticker) or {}
        products = (["现货"] if cov.get("spot") else []) + (["合约"] if cov.get("contract") else [])
        risks = []
        if cov.get("spot"):
            risks.append("现货：预测待核实｜公司行动未证实前不执行持仓、成本或订单调整")
        if cov.get("contract"):
            risks.append("合约：待核实｜公司行动本身仍是预测，未证实前不得执行")
        forecasts.append({
            "ticker": ticker, "etype": etype, "date": date, "event_id": "|".join(key),
            "days": days, "forecast": True, "confirmed": False, "manual_watch": True,
            "watching": True, "watch_note": watch.get("note", ""), "srcs": [],
            "products": products, "amount": None, "ratio": None, "value_display": "",
            "value_verified": False, "follow_up_mode": "verification", "risk": risks,
            "verification_kind": "forecast",
            "contract_action": {"status": "review"} if cov.get("contract") else {"status": "not_applicable"},
        })
    d["pending"] = pending
    d["forecasts"] = forecasts
    if isinstance(d.get("counts"), dict):
        d["counts"]["pending"] = len(pending)
        d["counts"]["forecasts"] = len(forecasts)
    return d


_FILING_EVENT_ID_RE = re.compile(
    r"(?<![A-Z0-9.-])[A-Z0-9.-]+\|filing\|\d{4}-\d{2}-\d{2}\|"
    r"[0-9a-f]{12}(?![A-Z0-9])",
    re.I,
)


def filing_resolution_target(text, data, ticker=None):
    """从 Pages 快照定位要结案的 filing，返回 ``(event, error)``。

    有完整 event_id 时精确匹配；只给代码+日期时仅在候选唯一时
    帮用户补齐 ID，同日多文件必须回到完整 ID。
    """
    candidates = {}
    for key in ("filing_updates", "pending", "calendar", "new"):
        for event in data.get(key, []) or []:
            if not isinstance(event, dict) or event.get("etype") != "filing":
                continue
            event_id = event.get("event_id") or ""
            if not _FILING_EVENT_ID_RE.fullmatch(event_id):
                continue
            is_review = (
                event.get("filing_relevant") is None
                or event.get("current_status") in {"review", "expired"}
                or event.get("kind") in {"review_pending", "expired"}
            )
            if is_review:
                candidates[event_id] = event

    raw = text or ""
    explicit = _FILING_EVENT_ID_RE.search(raw)
    if explicit:
        event_id = explicit.group(0)
        # 指纹为小写 hex；用快照中的原始 ID 进行大小写无关查找。
        hit_id = next((key for key in candidates if key.lower() == event_id.lower()), None)
        if hit_id:
            return candidates[hit_id], ""
        parts = event_id.split("|")
        event_ticker = parts[0].upper()
        if not cards.is_monitored_ticker(data, event_ticker):
            return None, f"{event_ticker} 不在当前公司行动监控范围"
        # 允许结案已从当前快照消失、但操作员保存了完整 ID 的积压项。
        return {
            "ticker": event_ticker,
            "etype": "filing",
            "date": parts[2],
            "event_id": event_id,
            "src_url": "",
        }, ""

    # 用户已经尝试粘贴 event_id 时，任何长度/字符错误都必须直接拒绝。
    # 不能再退回“代码+日期唯一匹配”，否则多一位指纹会被悄悄截断后结案。
    if "|filing|" in raw.lower():
        return None, "event_id 格式不完整或指纹长度错误；请从卡片复制完整 event_id"

    mdate = re.search(r"\d{4}-\d{2}-\d{2}", raw)
    date = mdate.group(0) if mdate else ""
    matches = [event for event in candidates.values()
               if (not ticker or event.get("ticker") == ticker)
               and (not date or event.get("date") == date)]
    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        ids = "\n".join(f"`{event['event_id']}`" for event in matches[:5])
        return None, f"同日有多份待核实 filing，请复制完整 event_id：\n{ids}"
    return None, "未找到可结案的 SEC 条款核验；请带完整 event_id，或使用唯一的代码+申报日"


def filing_resolution_status(text):
    """解析备案结论；不明确时 fail closed。"""
    raw = (text or "").lower()
    routine = any(word in raw for word in (
        "排除备案", "普通备案", "无需操作", "routine", "no action",
    ))
    confirmed = any(word in raw for word in (
        "确认备案", "公司行动", "confirmed", "confirm filing",
    ))
    if routine and confirmed:
        return ""
    if routine:
        return "routine"
    if confirmed:
        return "confirmed"
    return ""


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
        draw_month(data.get("calendar", []), path, business_date=data.get("business_date"))
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
    chat_id = None
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
        # 所有会改 GitHub 业务状态的指令先鉴权，再取快照、更不能先写回。
        # 白名单缺失也按拒绝处理，避免任何群成员借 Bot 的 GH_TOKEN 结案或改值。
        if cmd in WRITE_COMMANDS and not write_authorized(sender_oid):
            send_card(chat_id, cards.write_permission_card(SITE_URL))
            return
        d = fetch_data()
        clean_command = re.sub(r"@_user_\d+|@_all", "", text or "").strip().lower()
        explicit_help = any(
            ((not kw.isascii() and clean_command.startswith(kw.lower())) or
             re.match(rf"^{re.escape(kw.lower())}(?:\s|[:：,，、]|$)", clean_command))
            for command in cards.COMMANDS if command["key"] == "help"
            for kw in command["kw"]
        )
        snapshot_problem = cards.validate_snapshot(d)
        # 帮助、需求提报和审计留痕不依赖 Pages 快照；其余业务查询/写回必须
        # fail closed，不能把网络错误或旧 schema 冒充成“当前无风险”。
        if snapshot_problem and not (
            cmd in ("request", "audit") or (cmd == "help" and explicit_help)
        ):
            send_card(chat_id, cards.unavailable_card(snapshot_problem, SITE_URL))
            return
        refs_ir = (d.get("refs") if isinstance(d, dict) and isinstance(d.get("refs"), dict) else None)
        ticker = cards.find_ticker(text, d)
        # 查代码:显式『查』指令,或直接发了一个已覆盖的代码(未命中其它指令时)
        if cmd == "filing_resolve":
            status = filing_resolution_status(text)
            target, error = filing_resolution_target(text, d, ticker)
            if not status:
                send_card(chat_id, cards.filing_resolution_card(
                    False, "请明确选择「公司行动」或「普通备案/无需操作」。",
                    site_url=SITE_URL,
                ))
                return
            if not target:
                send_card(chat_id, cards.filing_resolution_card(
                    False, error, status=status, site_url=SITE_URL,
                ))
                return
            event_id = target["event_id"]
            src_url = target.get("src_url") or target.get("sec_url") or target.get("url") or ""
            print(f"[msg] chat={chat_id} -> filing_resolve {event_id} {status}")
            ok, msg = ack.resolve_filing_review(
                event_id, status,
                ticker=target.get("ticker", ""), date=target.get("date", ""),
                src_url=src_url,
            )
            send_card(chat_id, cards.filing_resolution_card(
                ok, msg, event_id, status, SITE_URL,
            ))
            return
        if cmd == "forecast":
            clean = re.sub(r"@_user_\d+|@_all", "", text or "")
            m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", clean)
            date = m.group(1) if m else ""
            if ticker and not cards.is_monitored_ticker(d, ticker):
                send_card(chat_id, cards.forecast_mark_card(
                    False, f"{ticker} 是当前覆盖中的非公司行动资产，不能标记为分红/公司行动预测。",
                    ticker, date, SITE_URL))
                return
            if not (ticker and date):
                send_card(chat_id, cards.forecast_card(d, SITE_URL))
                return
            etype = "dividend"
            event_src_url = ""
            for key in ("forecasts", "pending", "calendar"):
                hit = next((x for x in d.get(key, []) or []
                            if x.get("ticker") == ticker and x.get("date") == date), None)
                if hit:
                    etype = hit.get("etype") or etype
                    event_src_url = hit.get("src_url") or hit.get("url") or ""
                    break
            note = clean
            for kw in ("观察", "预测", "等待宣告", "watch"):
                note = re.sub(kw, "", note, flags=re.I)
            note = re.sub(rf"\b{re.escape(ticker)}\b", "", note, flags=re.I)
            note = note.replace(date, "").strip(" :：")
            by_name = get_user_name(sender_oid)
            print(f"[msg] chat={chat_id} -> forecast {ticker} {etype} @{date}")
            ok, msg = ack.add_forecast(ticker, etype, date, by=sender_oid or "",
                                       by_name=by_name, note=note, refs_ir=refs_ir,
                                       src_url=event_src_url)
            send_card(chat_id, cards.forecast_mark_card(ok, msg, ticker, date, SITE_URL))
            return
        if cmd == "confirm":
            clean = re.sub(r"@_user_\d+|@_all", "", text or "")  # 去掉 @ 占位符再取数值,避免误读
            # 先摘出日期(YYYY-MM-DD)再取数值 —— 否则「2026」会被当成金额。
            # 同一标的可能有多条不同值的异常，必须能用日期指定是哪一条。
            mdate = re.search(r"\d{4}-\d{2}-\d{2}", clean)
            date = mdate.group(0) if mdate else None
            rest = clean.replace(date, "") if date else clean
            # 完整保留拆/合股 new:old；否则 `1:10` 会被静默截成 `1`，
            # 下游再误解成 1:1，直接影响 3% 操作门槛。
            value, value_token = ack.parse_confirm_value(rest)

            # 备注:去掉指令词/代码/值之后剩下的自由文字(如「已比对公司 8-K」)
            note = rest
            if value_token:
                note = note.replace(value_token, "", 1)
            for _kw in ("确认", "confirm", "已核对"):
                note = re.sub(_kw, "", note, flags=re.I)
            if ticker:
                note = re.sub(rf"\b{re.escape(ticker)}\b", "", note, flags=re.I)
            note = note.strip(" :：,，、-—\t")

            etype = None
            if date:
                # 指定日期时先收集所有同日事件；比例值优先匹配 split，避免同日分红
                # 与拆股时被列表顺序带到错误类型。
                candidates = []
                for key in ("conflicts", "pending", "calendar", "gaps"):
                    for c in d.get(key, []) or []:
                        if c.get("ticker") == ticker and c.get("date") == date:
                            candidate = c.get("etype")
                            if candidate and candidate not in candidates:
                                candidates.append(candidate)
                preferred = "split" if value and ":" in value else "dividend"
                etype = preferred if preferred in candidates else (candidates[0] if len(candidates) == 1 else None)
            else:
                # 没给日期:默认取该标的的第一条冲突(多条不同值时,建议带上日期)
                for c in d.get("conflicts", []) or []:
                    if c.get("ticker") == ticker:
                        etype, date = c.get("etype"), c.get("date")
                        break
            print(f"[msg] chat={chat_id} text={text!r} -> confirm {ticker} {value} @{date}")
            if not ticker:
                send_card(chat_id, cards.confirm_card(
                    False, "没认出代码。用法:`确认 代码 [正确值] [日期] [备注]`,例:`确认 AAPL 0.26 2026-08-11 已比对公司公告`",
                    site_url=SITE_URL))
                return
            if not cards.is_monitored_ticker(d, ticker):
                send_card(chat_id, cards.confirm_card(
                    False, f"{ticker} 是当前覆盖中的非公司行动资产，不能执行人工确认。",
                    ticker=ticker, site_url=SITE_URL))
                return
            if not (etype and date):
                send_card(chat_id, cards.confirm_card(
                    False,
                    "没定位到唯一的具体事件。请带事件日期；若同日既有分红又有拆股，拆/合股请使用完整 `新股数:旧股数`。",
                    ticker=ticker,
                    site_url=SITE_URL,
                ))
                return
            if value and ":" in value and etype != "split":
                send_card(chat_id, cards.confirm_card(
                    False,
                    "只有拆股/合股事件可使用 `新股数:旧股数`，例如 `确认 XYZ 1:10 2026-09-10`。",
                    ticker=ticker,
                    site_url=SITE_URL,
                ))
                return
            # ADR 防呆:若确认的值像「净额(税后)」——低于该事件毛额约 5% 以上——就警告(仍记录)
            warn = ""
            _ag = None
            for c in (d.get("conflicts", []) or []):
                if (c.get("ticker") == ticker and c.get("etype") == etype
                        and c.get("date") == date):
                    _ag = c.get("adr_gross")
                    break
            try:
                if _ag and value is not None and float(value) < float(_ag) * 0.95:
                    warn = (f"⚠️ 你确认的 **{value}** 像是**净额(税后)**;该 ADR **毛额(税前)约 {_ag}**。"
                            f"我们认毛额 —— 若填错请用毛额重发一次(会覆盖)。")
            except (TypeError, ValueError):
                pass
            by_name = get_user_name(sender_oid)
            event_src_url = ""
            for key in ("conflicts", "pending", "calendar", "gaps"):
                hit = next((x for x in d.get(key, []) or []
                            if x.get("ticker") == ticker and x.get("etype") == etype
                            and x.get("date") == date), None)
                if hit:
                    event_src_url = hit.get("src_url") or hit.get("url") or ""
                    break
            ok, msg = ack.add_ack(ticker, value, etype, date,
                                  by=sender_oid or "", by_name=by_name, note=note,
                                  refs_ir=refs_ir, src_url=event_src_url)
            send_card(chat_id, cards.confirm_card(ok, msg, ticker, value, SITE_URL, date, etype, warn))
            return
        if cmd == "audit":
            # 留痕库:拉最近确认记录(可只看某个标的)。经 GH API 读 data/ack_log.json
            log = ack.get_ack_log(limit=200)
            audit_ticker = ticker
            if not audit_ticker:
                # 审计入口刻意不依赖 Pages：快照故障时仍能查留痕，已退出当前
                # coverage 的历史代码也应可筛选，不能退化成返回全部记录。
                known_log_tickers = {str(e.get("ticker") or "").upper() for e in log}
                tokens = re.findall(r"[A-Za-z][A-Za-z0-9.-]*", text or "")
                audit_ticker = next(
                    (token.upper() for token in tokens if token.upper() in known_log_tickers),
                    None,
                )
            if audit_ticker:
                log = [e for e in log
                       if str(e.get("ticker") or "").upper() == audit_ticker]
            print(f"[msg] chat={chat_id} -> audit ticker={audit_ticker} n={len(log)}")
            send_card(chat_id, cards.audit_card(log[:15], SITE_URL, audit_ticker))
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
        if chat_id:
            try:
                send_card(chat_id, cards.unavailable_card(
                    "问答助手处理消息时发生内部错误；本次未输出业务结论，请稍后重试。",
                    SITE_URL,
                ))
            except Exception as send_error:
                print("on_message error card failed:", send_error)


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
