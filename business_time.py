# -*- coding: utf-8 -*-
"""CA Monitor 的统一业务时钟。

公司行动日期均按美股市场日解释。GitHub Actions、开发机和部署容器可能运行在
不同系统时区，因此业务判断不能使用 ``date.today()``。
"""
import datetime as dt
from zoneinfo import ZoneInfo


MARKET_TIMEZONE = "America/New_York"
_MARKET_TZ = ZoneInfo(MARKET_TIMEZONE)


def now(instant=None):
    """返回 America/New_York 的带时区时间；``instant`` 仅供确定性测试。"""
    instant = instant or dt.datetime.now(dt.timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("instant must be timezone-aware")
    return instant.astimezone(_MARKET_TZ)


def today(instant=None):
    """返回当前美东业务日。"""
    return now(instant).date()
