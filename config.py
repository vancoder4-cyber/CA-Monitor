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

# ---- 业务范围 ----
# 现货:24 支美股个股
SPOT_TICKERS = {
    "MU", "SNDK", "NVDA", "TSLA", "AMD", "INTC", "MSFT", "AAPL",
    "AMZN", "GOOGL", "META", "AVGO", "MRVL", "PLTR", "LLY", "NBIS",
    "HOOD", "CRWV", "RKLB", "MSTR", "COIN", "CRCL", "HIMS", "SPCX",
}
# 合约:22(截图 23 行去掉已下架的 SOXL)
CONTRACT_TICKERS = {
    "MU", "SNDK", "MRVL", "INTC", "NVDA", "CRCL", "SPCX", "AMD", "MSTR", "TSLA", "GOOGL",  # 个股
    "QQQ", "EWY", "DRAM",                                                                   # ETF
    "XAU", "WTI", "XAG", "BRENTOIL", "NATGAS", "XCU", "CBRS", "SKHYNIX",                    # 商品/海外
}

# 标的类型:equity(个股) / etf / commodity(商品·外汇) / foreign(海外股)
# 只有 equity 和 etf 抓公司行动;commodity/foreign 列入覆盖但标"不适用"
ASSET_TYPE = {
    "QQQ": "etf", "EWY": "etf", "DRAM": "etf",
    "XAU": "commodity", "WTI": "commodity", "XAG": "commodity", "BRENTOIL": "commodity",
    "NATGAS": "commodity", "XCU": "commodity", "CBRS": "commodity",
    "SKHYNIX": "foreign",
}

def asset_type(tk):
    return ASSET_TYPE.get(tk, "equity")

def is_monitored(tk):
    return asset_type(tk) in ("equity", "etf")

# 全部资产(现货 ∪ 合约),用于"资产覆盖"视图
ALL_ASSETS = sorted(SPOT_TICKERS | CONTRACT_TICKERS)
# 实际抓公司行动的标的 = 个股 + ETF(商品/海外不抓)
TICKERS = sorted([t for t in ALL_ASSETS if is_monitored(t)])

NAMES = {
    "MU": "美光科技", "SNDK": "闪迪", "NVDA": "英伟达", "TSLA": "特斯拉",
    "AMD": "超威半导体", "INTC": "英特尔", "MSFT": "微软", "AAPL": "苹果",
    "AMZN": "亚马逊", "GOOGL": "谷歌A类", "META": "Meta", "AVGO": "博通",
    "MRVL": "迈威尔科技", "PLTR": "Palantir", "LLY": "礼来", "NBIS": "Nebius",
    "HOOD": "Robinhood", "CRWV": "CoreWeave", "RKLB": "火箭实验室",
    "MSTR": "微策略", "COIN": "Coinbase", "CRCL": "Circle",
    "HIMS": "Hims & Hers", "SPCX": "SpaceX",
    # 合约新增
    "QQQ": "纳指100 ETF", "EWY": "韩国 ETF", "XAU": "黄金", "WTI": "WTI原油",
    "XAG": "白银", "BRENTOIL": "布伦特原油", "NATGAS": "天然气", "XCU": "铜",
    "DRAM": "内存 ETF", "CBRS": "(合约)", "SKHYNIX": "SK海力士",
}

# ---- API keys ----
# 全部从环境变量 / .env 读取,代码里不留明文(避免提交到 GitHub)。
# 本地用:复制 .env.example 为 .env 并填入你的 key(.env 已在 .gitignore)。
_KEY_NAMES = ["ALPHAVANTAGE", "FMP", "FINNHUB", "TIINGO", "ALPACA_KEY_ID", "ALPACA_SECRET",
              "FINX_USER", "FINX_PASS", "FINX_BASE"]

def get_keys():
    return {k: os.environ.get(k, "") for k in _KEY_NAMES}

# ---- FINX (TRKD-HS) 静态数据 API ----
# 第 8 源,JWT 认证。凭证只走环境变量(FINX_USER/FINX_PASS),代码里不留明文。
# 接口仍在调整中(供方告知约 2 周、且基于 demo),故:未配置凭证 → 该源静默跳过,不影响其它源。
# 正式环境 base 默认如下;UAT 用 FINX_BASE 覆盖为 https://finx.uat.platform.trkd-hs.com/finx-api
FINX_BASE_DEFAULT = "https://finx.platform.trkd-hs.com/finx-api"

# FINX 用 RIC(路透代码,如 TSLA.O)。多数标的在 Nasdaq(.O),个别在 NYSE(.N)/NYSE Arca(.K)。
# 下面是按上市所给的覆盖表;拿不到准确 RIC 的留作默认 .O。接口稳定后按实际可调。
FINX_RIC = {
    # NYSE(.N)
    "LLY": "LLY.N", "CRCL": "CRCL.N", "RKLB": "RKLB.O", "HOOD": "HOOD.O",
    # NYSE Arca ETF(.K / .P,先按 .K)
    "EWY": "EWY.K",
    # 其余默认 .O(Nasdaq):MU/SNDK/NVDA/TSLA/AMD/INTC/MSFT/AAPL/AMZN/GOOGL/META/
    #   AVGO/MRVL/PLTR/NBIS/CRWV/MSTR/COIN/HIMS/QQQ/DRAM
}

def finx_ric(ticker):
    """ticker -> FINX RIC。商品/海外不抓(返回 None);其余按覆盖表或默认 .O。"""
    if not is_monitored(ticker):
        return None
    return FINX_RIC.get(ticker, f"{ticker}.O")

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

# ---- 预警节奏(距除息日天数,临近时只触发"最接近的一轮")----
ALERT_ROUNDS = [30, 14, 7, 3, 1]
# 以哪个日期作为预警基准
ALERT_ANCHOR = "ex_date"
# 已公告未执行的事件:是否每次跑都持续推送(直到执行)
PENDING_ALWAYS_PUSH = True

# 各轮「运营」操作文案(随天数升级:30/14 仅知会,7/3/1 升级为催办)
ROUND_OPS = {
    30: "提前知会:距除息约 30 天,请运营留意并排入计划。",
    14: "提前知会:距除息约 14 天,请运营确认本次活动安排。",
    7:  "⏱ 催办:距除息 7 天 —— 请运营开始准备相关文案,并明确「具体哪天」执行各项操作、完成排期。",
    3:  "⏱ 催办·收尾:距除息 3 天 —— 请确保相关文案全部写完。",
    1:  "⏱ 最后确认:距除息仅 1 天 —— 确保运营文案已就绪,并备好定时发送事宜。",
}
# 风控文案:待风控团队明确(占位,各轮都带)
ROUND_RISK_TBD = "风控提醒:待风控团队明确(占位)"

def round_copy(rnd):
    return ROUND_OPS.get(rnd, ""), ROUND_RISK_TBD

# ---- 产品归属(用于风控运营提示;SPOT_TICKERS / CONTRACT_TICKERS 见文件上方业务范围)----
def product_tags(ticker):
    tags = []
    if ticker in SPOT_TICKERS:
        tags.append("现货")
    if ticker in CONTRACT_TICKERS:
        tags.append("合约")
    return tags

# 风控运营提示:按 事件类型 × 产品 给默认动作(按你内部流程改)
RISK_NOTES = {
    "dividend": {
        "contract": "合约:核对价格基准/资金费率是否需调整,除息日防价格跳空引发异常强平",
        "spot": "现货:除息日成本基准调整,持仓与对账核对",
    },
    "split": {
        "contract": "合约:调整合约乘数/持仓数量/委托价,重点防穿仓与挂单错位",
        "spot": "现货:按比例调整持仓与未成交挂单,提前公告用户",
    },
    "filing": {
        "contract": "合约:评估并购/退市影响,必要时暂停开仓、移仓或强制结算",
        "spot": "现货:评估下架/暂停充提与交易,公告用户",
    },
}

def risk_note(ticker, etype):
    """按产品归属拼出风控运营提示。"""
    notes = RISK_NOTES.get(etype, {})
    out = []
    if ticker in CONTRACT_TICKERS and notes.get("contract"):
        out.append(notes["contract"])
    if ticker in SPOT_TICKERS and notes.get("spot"):
        out.append(notes["spot"])
    return out

# ---- 时间范围 ----
LOOKBACK_DAYS = 200    # 回看多久(用于核对历史一致性)
LOOKAHEAD_DAYS = 120   # 前看多久(未来事件)

# 8-K 的 Item 代码 → 中文事件类型
SEC_8K_ITEMS = {
    "1.01": "签订重大协议", "1.02": "终止重大协议", "1.03": "破产/接管",
    "2.01": "完成收购/资产处置", "2.02": "业绩/经营结果(财报)", "2.03": "产生重大债务",
    "2.04": "债务加速/触发", "2.05": "重组成本", "2.06": "资产减值",
    "3.01": "退市/不符合上市标准", "3.02": "未注册股票发行", "3.03": "证券持有人权利变更",
    "4.01": "会计师变更", "4.02": "财报不可依赖",
    "5.01": "控制权变更", "5.02": "董事/高管变动", "5.03": "章程/财年变更",
    "5.07": "股东投票结果", "5.08": "股东提名事项",
    "7.01": "Reg FD 披露", "8.01": "其他重大事件", "9.01": "财务报表与附件",
}
# 与「公司行动 / 需发公告」相关的 8-K Item
SEC_8K_CA_ITEMS = {"1.01", "2.01", "3.01", "3.03", "5.01", "5.07", "8.01"}

def describe_8k(items_str):
    """8-K items 串 -> (中文描述列表, 是否公司行动相关)。"""
    codes = [c.strip() for c in (items_str or "").split(",") if c.strip()]
    descs = [f"{c} {SEC_8K_ITEMS.get(c, '')}".strip() for c in codes]
    relevant = any(c in SEC_8K_CA_ITEMS for c in codes)
    return descs, relevant

# 哪些 SEC 表格视为公司行动信号
SEC_FORMS_OF_INTEREST = {
    "8-K": "重大事件",
    "25": "退市", "25-NSE": "退市(交易所)",
    "425": "并购要约/沟通", "S-4": "并购注册", "DEFM14A": "并购股东投票",
    "8-K12B": "证券变更", "15-12B": "注销登记/退市",
    "SC TO-I": "要约收购", "SC 14D9": "要约收购回应",
}
