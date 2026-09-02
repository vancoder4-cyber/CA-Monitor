# -*- coding: utf-8 -*-
"""独立部署 Bot 使用的美股市场业务时钟。"""
import datetime as dt
from zoneinfo import ZoneInfo


MARKET_TIMEZONE = "America/New_York"
_MARKET_TZ = ZoneInfo(MARKET_TIMEZONE)


def now(instant=None):
    instant = instant or dt.datetime.now(dt.timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("instant must be timezone-aware")
    return instant.astimezone(_MARKET_TZ)


def today(instant=None):
    return now(instant).date()
