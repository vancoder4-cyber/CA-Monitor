# -*- coding: utf-8 -*-
"""配置:标的清单、API key、容差、预警节奏。

API key 建议用环境变量覆盖(见 get_keys),避免明文留在代码里。
"""
import os

# ---- 极简 .env 加载(无需第三方依赖)----
def _load_dotenv():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

_load_dotenv()

# ---- 标的清单(24 支)----
TICKERS = [
    "MU", "SNDK", "NVDA", "TSLA", "AMD", "INTC", "MSFT", "AAPL",
    "AMZN", "GOOGL", "META", "AVGO", "MRVL", "PLTR", "LLY", "NBIS",
    "HOOD", "CRWV", "RKLB", "MSTR", "COIN", "CRCL", "HIMS", "SPCX",
]

NAMES = {
    "MU": "美光科技", "SNDK": "闪迪", "NVDA": "英伟达", "TSLA": "特斯拉",
    "AMD": "超威半导体", "INTC": "英特尔", "MSFT": "微软", "AAPL": "苹果",
    "AMZN": "亚马逊", "GOOGL": "谷歌A类", "META": "Meta", "AVGO": "博通",
    "MRVL": "迈威尔科技", "PLTR": "Palantir", "LLY": "礼来", "NBIS": "Nebius",
    "HOOD": "Robinhood", "CRWV": "CoreWeave", "RKLB": "火箭实验室",
    "MSTR": "微策略", "COIN": "Coinbase", "CRCL": "Circle",
    "HIMS": "Hims & Hers", "SPCX": "SpaceX",
}

# ---- API keys ----
# 全部从环境变量 / .env 读取,代码里不留明文(避免提交到 GitHub)。
# 本地用:复制 .env.example 为 .env 并填入你的 key(.env 已在 .gitignore)。
_KEY_NAMES = ["ALPHAVANTAGE", "FMP", "FINNHUB", "TIINGO", "ALPACA_KEY_ID", "ALPACA_SECRET"]

def get_keys():
    return {k: os.environ.get(k, "") for k in _KEY_NAMES}

# SEC 要求 User-Agent 带联系邮箱
SEC_UA = os.environ.get("SEC_UA", "ca-monitor vancoder4@gmail.com")

# ---- 核对策略 ----
# 跨源把"同一事件"归组的时间窗(天):除权日相差在此范围内视为同一事件候选
GROUP_WINDOW_DAYS = 5
# 零容忍:归组后,比对字段只要有任何差异即判为冲突
ZERO_TOLERANCE = True
# 比对哪些字段(分红)
DIV_COMPARE_FIELDS = ["ex_date", "record_date", "pay_date", "amount"]
# 比对哪些字段(拆股)
SPLIT_COMPARE_FIELDS = ["ex_date", "ratio"]

# ---- 预警节奏(距关键日期天数,各触发一轮)----
ALERT_ROUNDS = [30, 14, 7, 3, 1]
# 以哪个日期作为预警基准
ALERT_ANCHOR = "ex_date"

# ---- 时间范围 ----
LOOKBACK_DAYS = 200    # 回看多久(用于核对历史一致性)
LOOKAHEAD_DAYS = 120   # 前看多久(未来事件)

# 哪些 SEC 表格视为公司行动信号
SEC_FORMS_OF_INTEREST = {
    "8-K": "重大事件",
    "25": "退市", "25-NSE": "退市(交易所)",
    "425": "并购要约/沟通", "S-4": "并购注册", "DEFM14A": "并购股东投票",
    "8-K12B": "证券变更", "15-12B": "注销登记/退市",
    "SC TO-I": "要约收购", "SC 14D9": "要约收购回应",
}
