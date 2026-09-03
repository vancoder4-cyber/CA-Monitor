# -*- coding: utf-8 -*-
"""人工确认写回:把『已确认』写进 repo 的 data/acknowledged.json(GitHub Contents API)。

需要环境变量(在 Railway 配置;细粒度 PAT 只需对本 repo 的 Contents 有读写权限):
    GH_TOKEN   —— 细粒度 Personal Access Token(Contents: Read and write)
    GH_REPO    —— 形如 vancoder4-cyber/CA-Monitor(默认即此)
    GH_BRANCH  —— 默认 main

run.py 会读取 data/acknowledged.json,把对应冲突标为「已人工确认」:停止报警 + 网页 finalize。
"""
import os
import json
import base64
import datetime as dt
import hashlib
import re

import requests

GH_TOKEN = os.environ.get("GH_TOKEN", "").strip()
GH_REPO = os.environ.get("GH_REPO", "vancoder4-cyber/CA-Monitor").strip()
GH_BRANCH = os.environ.get("GH_BRANCH", "main").strip()
ACK_PATH = "data/acknowledged.json"   # 当前生效值(同标的+同日去重,pipeline 读这个)
LOG_PATH = "data/ack_log.json"        # 留痕库:只追加、永不删,记录每一次确认(含改值前后)
FORECAST_PATH = "data/forecast_watch.json"  # 人工观察的预测；不是确认，不改变数据本身
FILING_RESOLUTION_PATH = "data/filing_review_resolutions.json"  # 按稳定 event_id 关闭 SEC 条款核验
API = "https://api.github.com"
_BJ = dt.timezone(dt.timedelta(hours=8))
_HERE = os.path.dirname(os.path.abspath(__file__))
_ETF_TICKERS = {"QQQ", "EWY", "DRAM", "TQQQ", "MVLL"}


def parse_confirm_value(text):
    """返回 (规范值, 原始命中文本)；拆/合股比例必须完整保留 new:old。"""
    raw = text or ""
    ratio = re.search(
        r"(?<![\d.])(\d+(?:\.\d+)?)\s*[:：]\s*(\d+(?:\.\d+)?)(?![\d.])",
        raw,
    )
    if ratio:
        return f"{ratio.group(1)}:{ratio.group(2)}", ratio.group(0)
    number = re.search(r"\d+(?:\.\d+)?", raw)
    return (number.group(0), number.group(0)) if number else (None, None)


def _headers():
    return {"Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _get_file(path=ACK_PATH):
    """返回 (data:list, sha or None)。文件不存在则 ([], None)。"""
    url = f"{API}/repos/{GH_REPO}/contents/{path}?ref={GH_BRANCH}"
    r = requests.get(url, headers=_headers(), timeout=15)
    if r.status_code == 200:
        j = r.json()
        try:
            data = json.loads(base64.b64decode(j["content"]).decode("utf-8"))
        except Exception:
            data = []
        return (data if isinstance(data, list) else []), j.get("sha")
    return [], None


def _put_file(path, data, sha, message):
    body = {"message": message,
            "content": base64.b64encode(
                json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")).decode("utf-8"),
            "branch": GH_BRANCH}
    if sha:
        body["sha"] = sha
    r = requests.put(f"{API}/repos/{GH_REPO}/contents/{path}",
                     headers=_headers(), json=body, timeout=20)
    return r


def _load_refs_ir():
    """refs.json 的 ir_dividend(公司官方分红页),没有就空。"""
    try:
        p = os.path.join(os.path.dirname(_HERE), "refs.json")
        return json.load(open(p, encoding="utf-8")).get("ir_dividend", {})
    except Exception:
        return {}


def _sec_company_url(ticker):
    return ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
            f"&ticker={ticker}&type=&dateb=&owner=include&count=40")


def authoritative_source(ticker, etype, refs_ir=None, src_url=""):
    """给一条确认自动带出『最权威的核对来源』链接,确认人点开核对即可。
    分红优先使用公司分红 IR；拆股/filing 不能复用分红页，优先本事件原文，
    再回退 SEC EDGAR 该标的全部备案(8-K 普通股 / 6-K 外国发行人 ADR 都能覆盖)。
    不用 Nasdaq 分红页 —— 它是 JS 渲染、常空白,且不覆盖 NYSE/ADR(HPE、BABA 都点不出)。"""
    if etype == "dividend":
        ir = (refs_ir if refs_ir is not None else _load_refs_ir()).get(ticker) or ""
        if ir:
            return ir
    return src_url or _sec_company_url(ticker)


def quick_look(ticker, etype):
    """快速核对『数值对不对』用的聚合页(服务端渲染、覆盖 US+ADR,比 Nasdaq 稳)。"""
    tk = (ticker or "").upper()
    asset_path = "etf" if tk in _ETF_TICKERS else "stocks"
    base = f"https://stockanalysis.com/{asset_path}/{tk.lower()}"
    return f"{base}/dividend/" if etype == "dividend" else f"{base}/"


def verify_link(ticker, etype, src_url="", refs_ir=None):
    """『核对来源』置信度分级(1 没有看 2,2 没有看 3),返回 (url, label, tier):
      T1 公司 IR 分红页(refs.json,最权威·第一方·直接显示宣告值)
      T2 具体 SEC filing(只有可靠的才用:并购/退市源给的真实 url;分红一般没有)
      T3 聚合页 stockanalysis(服务端渲染,美股+ADR 都显示 USD 分红历史,快速核对)
    注意:ADR 的 USD/ADR 是存托行折算,SEC 里没有,所以那种情况 T3 反而比 SEC 有用。"""
    tk = ticker or ""
    if etype == "dividend":
        ir = (refs_ir if refs_ir is not None else _load_refs_ir()).get(tk) or ""
        if ir:
            return ir, "公司IR·最权威", 1
    if src_url:
        return src_url, "SEC原文", 2
    if etype in ("split", "filing"):
        return _sec_company_url(tk), "SEC·公司备案", 2
    return quick_look(tk, etype), "聚合页·第三方(快速核对)", 3


def get_acks():
    """读取当前生效的人工确认 acknowledged.json(bot 用来把已确认项即时从卡片剔除)。
    无 token/文件 → []。"""
    if not GH_TOKEN:
        return []
    try:
        data, _ = _get_file(ACK_PATH)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def get_forecasts():
    """读取预测观察项，供交互机器人在 Pages 尚未刷新时即时叠加状态。"""
    if not GH_TOKEN:
        return []
    try:
        data, _ = _get_file(FORECAST_PATH)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def get_filing_resolutions():
    """读取 SEC filing 条款核验的人工结论。

    必须使用完整 event_id；同一标的同一天可能有多份 6-K，不得
    退化成 ticker + date 宽匹配。
    """
    if not GH_TOKEN:
        return []
    try:
        data, _ = _get_file(FILING_RESOLUTION_PATH)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def get_ack_log(limit=None):
    """读取留痕库(只追加日志),按时间**倒序**返回(最新在前)。无 token/文件则 []。"""
    if not GH_TOKEN:
        return []
    try:
        data, _ = _get_file(LOG_PATH)
        data = list(reversed(data))
        return data[:limit] if limit else data
    except Exception:
        return []


def add_ack(ticker, value=None, etype=None, date=None, by="lark", by_name="", note="", *,
            refs_ir=None, src_url=""):
    """记录一条确认。写两处:留痕库(只追加)+ 生效值(去重)。返回 (ok, msg)。"""
    if not GH_TOKEN:
        return False, "未配置 GH_TOKEN —— 请在 Railway 加一个对本仓库 Contents 有写权限的细粒度 PAT"
    try:
        now = dt.datetime.now(dt.timezone.utc)
        # 1) 取当前生效值(为了留痕里记录『从旧值改成新值』)
        data, sha = _get_file(ACK_PATH)
        prev = next((e.get("value") for e in data
                     if e.get("ticker") == ticker and e.get("etype") == etype
                     and e.get("date") == date), None)

        # 2) 先写留痕库(只追加,永不删)—— 审计的可信底账,必须成功
        log, log_sha = _get_file(LOG_PATH)
        entry = {
            "at_bj": now.astimezone(_BJ).isoformat(timespec="seconds"),
            "at_utc": now.isoformat(timespec="seconds"),
            "ticker": ticker, "etype": etype, "date": date,
            "value": value, "prev_value": prev,
            "by_name": by_name or "", "by": by or "",
            "source": authoritative_source(ticker, etype, refs_ir=refs_ir, src_url=src_url),
            "note": (note or "").strip(),
            "action": "confirm",
        }
        log.append(entry)
        rlog = _put_file(LOG_PATH, log, log_sha,
                         f"ack-log: {ticker} {value if value is not None else ''} @{date or ''}".strip())
        if rlog.status_code not in (200, 201):
            return False, f"留痕写入失败 HTTP {rlog.status_code}: {rlog.text[:140]}"

        # 3) 再更新生效值(同标的+同类型+同日期去重替换)—— 同日分红与拆股不能互相覆盖
        data = [e for e in data if not (
            e.get("ticker") == ticker and e.get("etype") == etype and e.get("date") == date
        )]
        data.append({"ticker": ticker, "value": value, "etype": etype, "date": date,
                     "by": by, "by_name": by_name or "", "at": now.isoformat(timespec="seconds")})
        rack = _put_file(ACK_PATH, data, sha, f"ack: {ticker} {value if value is not None else ''}".strip())
        if rack.status_code not in (200, 201):
            return False, (f"确认仅留痕、未生效：生效值写入失败 HTTP {rack.status_code}: "
                           f"{rack.text[:140]}；报警不会解除，请重试。")
        chg = f"(原 {prev} → {value})" if prev not in (None, "", value) else ""
        return True, f"已记录确认并留痕{chg}"
    except Exception as e:
        return False, f"确认写入异常: {e}"


def add_forecast(ticker, etype, date, by="lark", by_name="", note="", *, refs_ir=None,
                 src_url=""):
    """把单源预测置为「观察中」。

    观察不会把事件当成已确认公司行动：流水线仍会持续抓取，后续有宣告日/第二源时自动升级。
    """
    if not GH_TOKEN:
        return False, "未配置 GH_TOKEN —— 请在 Railway 加一个对本仓库 Contents 有写权限的细粒度 PAT"
    if not ticker or not etype or not date:
        return False, "用法:`观察 代码 日期 [备注]`,例:`观察 AAPL 2026-08-11 等待公司宣告`"
    try:
        now = dt.datetime.now(dt.timezone.utc)
        data, sha = _get_file(FORECAST_PATH)
        data = [e for e in data if not (e.get("ticker") == ticker and e.get("etype") == etype
                                        and e.get("date") == date)]
        data.append({"ticker": ticker, "etype": etype, "date": date, "status": "watching",
                     "by": by or "", "by_name": by_name or "", "note": (note or "").strip(),
                     "at": now.isoformat(timespec="seconds")})

        log, log_sha = _get_file(LOG_PATH)
        log.append({"at_bj": now.astimezone(_BJ).isoformat(timespec="seconds"),
                    "at_utc": now.isoformat(timespec="seconds"), "ticker": ticker,
                    "etype": etype, "date": date, "value": None, "prev_value": None,
                    "by_name": by_name or "", "by": by or "",
                    "source": authoritative_source(ticker, etype, refs_ir=refs_ir, src_url=src_url),
                    "note": (note or "").strip(), "action": "watch_forecast"})
        rlog = _put_file(LOG_PATH, log, log_sha, f"forecast-watch-log: {ticker} @{date}")
        if rlog.status_code not in (200, 201):
            return False, f"留痕写入失败 HTTP {rlog.status_code}: {rlog.text[:140]}"
        r = _put_file(FORECAST_PATH, data, sha, f"forecast-watch: {ticker} @{date}")
        if r.status_code not in (200, 201):
            return False, (f"观察仅留痕、未生效：观察状态写入失败 HTTP {r.status_code}: "
                           f"{r.text[:140]}；请重试。")
        return True, ("已标记为预测观察：临近时会推数据核验提醒，但不会进入正式执行催办；"
                      "出现公司宣告或第二个独立源时会自动升级并推送。")
    except Exception as e:
        return False, f"观察写入异常: {e}"


def resolve_filing_review(event_id, status, *, ticker="", date="", by="lark",
                          by_name="", note="", src_url=""):
    """把一条 SEC filing 核验明确结案为「公司行动」或「普通备案」。

    状态库按完整 event_id 去重；留痕库只追加。仓库是公开的，因此这条
    新链路只保存业务结论、时间和 SEC 来源，不持久化 Lark 身份或自由备注。
    ``by`` / ``by_name`` / ``note`` 仅为兼容旧调用保留，写入时主动丢弃。
    """
    if not GH_TOKEN:
        return False, "未配置 GH_TOKEN —— 请在 Railway 加一个对本仓库 Contents 有写权限的细粒度 PAT"
    if status not in {"confirmed", "routine"}:
        return False, "备案结论只能是「公司行动」或「普通备案/无需操作」"
    if not re.fullmatch(
            r"[A-Z0-9.-]+\|filing\|\d{4}-\d{2}-\d{2}\|[0-9a-f]{12}",
            event_id or ""):
        return False, "event_id 无效；请从条款核验卡片复制完整 ID"
    try:
        now = dt.datetime.now(dt.timezone.utc)
        data, sha = _get_file(FILING_RESOLUTION_PATH)
        previous = next(
            (e for e in data if isinstance(e, dict) and e.get("event_id") == event_id),
            None,
        )

        log, log_sha = _get_file(LOG_PATH)
        log.append({
            "at_bj": now.astimezone(_BJ).isoformat(timespec="seconds"),
            "at_utc": now.isoformat(timespec="seconds"),
            "ticker": ticker or event_id.split("|", 1)[0],
            "etype": "filing",
            "date": date or event_id.split("|")[2],
            "event_id": event_id,
            "value": status,
            "prev_value": (previous or {}).get("status"),
            "by_name": "",
            "by": "system",
            "source": src_url or _sec_company_url(ticker or event_id.split("|", 1)[0]),
            "note": "",
            "action": "resolve_filing_review",
        })
        rlog = _put_file(
            LOG_PATH, log, log_sha,
            f"filing-review-log: {event_id} -> {status}",
        )
        if rlog.status_code not in (200, 201):
            return False, f"留痕写入失败 HTTP {rlog.status_code}: {rlog.text[:140]}"

        data = [e for e in data
                if not (isinstance(e, dict) and e.get("event_id") == event_id)]
        data.append({
            "event_id": event_id,
            "ticker": ticker or event_id.split("|", 1)[0],
            "date": date or event_id.split("|")[2],
            "status": status,
            # 这是公开 Git 生效库：不保存 Lark open_id、姓名或自由备注。
            "source": src_url or "",
            "at": now.isoformat(timespec="seconds"),
        })
        result = _put_file(
            FILING_RESOLUTION_PATH, data, sha,
            f"filing-review: {event_id} -> {status}",
        )
        if result.status_code not in (200, 201):
            return False, (f"结论仅留痕、未生效：状态写入失败 HTTP {result.status_code}: "
                           f"{result.text[:140]}；核验提醒不会关闭，请重试。")
        label = "已确认为公司行动" if status == "confirmed" else "已判定为普通备案，本次无需操作"
        return True, (f"{label}；已按稳定 event_id 写入匿名业务留痕。"
                      "公开仓库不会保存操作员身份或自由备注。")
    except Exception as e:
        return False, f"备案结论写入异常: {e}"


REQ_PATH = "requests.md"


def add_request(text, by=""):
    """把需求追加到公开 repo 的 requests.md；不持久化 Lark 身份。"""
    if not GH_TOKEN:
        return False, "未配置 GH_TOKEN —— 请在 Railway 加一个对本仓库 Contents 有写权限的细粒度 PAT"
    try:
        url = f"{API}/repos/{GH_REPO}/contents/{REQ_PATH}?ref={GH_BRANCH}"
        r = requests.get(url, headers=_headers(), timeout=15)
        if r.status_code == 200:
            j = r.json()
            content = base64.b64decode(j["content"]).decode("utf-8")
            sha = j.get("sha")
        else:
            content = "# 需求提报汇总\n\n> 群里 @机器人 + 「需求 内容」自动追加到这里。\n"
            sha = None
        request_time = dt.datetime.now(dt.timezone.utc)
        ts = request_time.isoformat(timespec="minutes")
        request_id = hashlib.sha256(
            f"{request_time.isoformat(timespec='microseconds')}\0{text}".encode("utf-8")
        ).hexdigest()[:10]
        # requests.md 位于公开仓库。只保存匿名编号与需求正文，不写 open_id、姓名，
        # commit subject 也不能携带用户原文，避免 Git 历史形成第二份敏感副本。
        content += f"\n- [ ] {ts} · 编号 {request_id}\n  {text}\n"
        new_content = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        body = {"message": f"request: add bot submission {request_id}",
                "content": new_content, "branch": GH_BRANCH}
        if sha:
            body["sha"] = sha
        r = requests.put(f"{API}/repos/{GH_REPO}/contents/{REQ_PATH}",
                         headers=_headers(), json=body, timeout=20)
        if r.status_code in (200, 201):
            return True, "已收到需求"
        return False, f"写入失败 HTTP {r.status_code}: {r.text[:160]}"
    except Exception as e:
        return False, f"需求写入异常: {e}"
