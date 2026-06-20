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
ACK_PATH = "data/acknowledged.json"
API = "https://api.github.com"


def _headers():
    return {"Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _get_file():
    """返回 (data:list, sha or None)。文件不存在则 ([], None)。"""
    url = f"{API}/repos/{GH_REPO}/contents/{ACK_PATH}?ref={GH_BRANCH}"
    r = requests.get(url, headers=_headers(), timeout=15)
    if r.status_code == 200:
        j = r.json()
        try:
            data = json.loads(base64.b64decode(j["content"]).decode("utf-8"))
        except Exception:
            data = []
        return (data if isinstance(data, list) else []), j.get("sha")
    return [], None


def add_ack(ticker, value=None, etype=None, date=None, by="lark"):
    """记录一条确认。返回 (ok: bool, msg: str)。"""
    if not GH_TOKEN:
        return False, "未配置 GH_TOKEN —— 请在 Railway 加一个对本仓库 Contents 有写权限的细粒度 PAT"
    try:
        data, sha = _get_file()
        # 同标的 + 同日期 视为同一条,替换(去重)
        data = [e for e in data if not (e.get("ticker") == ticker and e.get("date") == date)]
        data.append({"ticker": ticker, "value": value, "etype": etype, "date": date,
                     "by": by, "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
        new_content = base64.b64encode(
            json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")).decode("utf-8")
        body = {"message": f"ack: {ticker} {value if value is not None else ''}".strip(),
                "content": new_content, "branch": GH_BRANCH}
        if sha:
            body["sha"] = sha
        r = requests.put(f"{API}/repos/{GH_REPO}/contents/{ACK_PATH}",
                         headers=_headers(), json=body, timeout=20)
        if r.status_code in (200, 201):
            return True, "已记录确认"
        return False, f"写入失败 HTTP {r.status_code}: {r.text[:160]}"
    except Exception as e:
        return False, f"确认写入异常: {e}"
