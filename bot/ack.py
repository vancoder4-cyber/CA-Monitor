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

import requests

GH_TOKEN = os.environ.get("GH_TOKEN", "").strip()
GH_REPO = os.environ.get("GH_REPO", "vancoder4-cyber/CA-Monitor").strip()
GH_BRANCH = os.environ.get("GH_BRANCH", "main").strip()
ACK_PATH = "data/acknowledged.json"   # 当前生效值(同标的+同日去重,pipeline 读这个)
LOG_PATH = "data/ack_log.json"        # 留痕库:只追加、永不删,记录每一次确认(含改值前后)
API = "https://api.github.com"
_BJ = dt.timezone(dt.timedelta(hours=8))
_HERE = os.path.dirname(os.path.abspath(__file__))


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


def authoritative_source(ticker, etype, refs_ir=None):
    """给一条确认自动带出『最权威的核对来源』链接,确认人点开核对即可。
    优先级:公司 IR(refs,最权威)→ SEC EDGAR 该标的全部备案(8-K 普通股 / 6-K 外国发行人 ADR 都能覆盖)。
    不用 Nasdaq 分红页 —— 它是 JS 渲染、常空白,且不覆盖 NYSE/ADR(HPE、BABA 都点不出)。"""
    ir = (refs_ir if refs_ir is not None else _load_refs_ir()).get(ticker) or ""
    # EDGAR 用 ticker= 参数直接按代码解析到公司,列出全部 filing(含 8-K/6-K),始终有内容
    edgar = (f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
             f"&ticker={ticker}&type=&dateb=&owner=include&count=40")
    return ir or edgar


def quick_look(ticker, etype):
    """快速核对『数值对不对』用的聚合页(服务端渲染、覆盖 US+ADR,比 Nasdaq 稳)。
    留痕里存的是 authoritative_source(权威原始出处);这个只给人肉 eyeball 用。"""
    tkl = (ticker or "").lower()
    return f"https://stockanalysis.com/stocks/{tkl}/dividend/"


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


def add_ack(ticker, value=None, etype=None, date=None, by="lark", by_name="", note=""):
    """记录一条确认。写两处:留痕库(只追加)+ 生效值(去重)。返回 (ok, msg)。"""
    if not GH_TOKEN:
        return False, "未配置 GH_TOKEN —— 请在 Railway 加一个对本仓库 Contents 有写权限的细粒度 PAT"
    try:
        now = dt.datetime.now(dt.timezone.utc)
        # 1) 取当前生效值(为了留痕里记录『从旧值改成新值』)
        data, sha = _get_file(ACK_PATH)
        prev = next((e.get("value") for e in data
                     if e.get("ticker") == ticker and e.get("date") == date), None)

        # 2) 先写留痕库(只追加,永不删)—— 审计的可信底账,必须成功
        log, log_sha = _get_file(LOG_PATH)
        entry = {
            "at_bj": now.astimezone(_BJ).isoformat(timespec="seconds"),
            "at_utc": now.isoformat(timespec="seconds"),
            "ticker": ticker, "etype": etype, "date": date,
            "value": value, "prev_value": prev,
            "by_name": by_name or "", "by": by or "",
            "source": authoritative_source(ticker, etype),
            "note": (note or "").strip(),
            "action": "confirm",
        }
        log.append(entry)
        rlog = _put_file(LOG_PATH, log, log_sha,
                         f"ack-log: {ticker} {value if value is not None else ''} @{date or ''}".strip())
        if rlog.status_code not in (200, 201):
            return False, f"留痕写入失败 HTTP {rlog.status_code}: {rlog.text[:140]}"

        # 3) 再更新生效值(同标的+同日期去重替换)—— pipeline 据此停报警
        data = [e for e in data if not (e.get("ticker") == ticker and e.get("date") == date)]
        data.append({"ticker": ticker, "value": value, "etype": etype, "date": date,
                     "by": by, "by_name": by_name or "", "at": now.isoformat(timespec="seconds")})
        rack = _put_file(ACK_PATH, data, sha, f"ack: {ticker} {value if value is not None else ''}".strip())
        if rack.status_code not in (200, 201):
            return True, "已留痕,但生效值写入失败(报警可能未即时消解),稍后会自动重试口径。"
        chg = f"(原 {prev} → {value})" if prev not in (None, "", value) else ""
        return True, f"已记录确认并留痕{chg}"
    except Exception as e:
        return False, f"确认写入异常: {e}"


REQ_PATH = "requests.md"


def add_request(text, by=""):
    """把需求追加到 repo 的 requests.md(供负责人汇总)。返回 (ok, msg)。"""
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
        ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
        content += f"\n- [ ] {ts} · 提报人 {by or '未知'}\n  {text}\n"
        new_content = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        body = {"message": f"需求提报: {text[:40]}", "content": new_content, "branch": GH_BRANCH}
        if sha:
            body["sha"] = sha
        r = requests.put(f"{API}/repos/{GH_REPO}/contents/{REQ_PATH}",
                         headers=_headers(), json=body, timeout=20)
        if r.status_code in (200, 201):
            return True, "已收到需求"
        return False, f"写入失败 HTTP {r.status_code}: {r.text[:160]}"
    except Exception as e:
        return False, f"需求写入异常: {e}"
