import datetime as dt
import json
import unittest

import business_time
from bot import business_time as bot_business_time
from bot import cards


UTC_BOUNDARY = dt.datetime(2026, 8, 29, 2, 48, tzinfo=dt.timezone.utc)


def _event(ticker, date, *, forecast=False, record=None, pay=None, decl=None):
    return {
        "ticker": ticker,
        "etype": "dividend",
        "date": date,
        "record": record,
        "pay": pay,
        "decl": decl,
        "forecast": forecast,
        "products": ["现货"],
        "references": [{"label": "官方", "url": "https://example.invalid/event"}],
    }


def _card_text(card):
    return json.dumps(card, ensure_ascii=False)


class BusinessTimeTests(unittest.TestCase):
    def test_utc_next_day_does_not_advance_new_york_business_date(self):
        # 02:48 UTC 已是 8 月 29 日，但美东仍为 8 月 28 日 22:48。
        expected = dt.date(2026, 8, 28)
        self.assertEqual(expected, business_time.today(UTC_BOUNDARY))
        self.assertEqual(expected, bot_business_time.today(UTC_BOUNDARY))


class BotWindowTests(unittest.TestCase):
    def test_week_counts_four_confirmed_anchor_events_once(self):
        data = {
            "business_date": "2026-08-31",
            "calendar": [
                _event("HD", "2026-09-03", record="2026-09-03"),
                _event("QCOM", "2026-09-03", record="2026-09-03"),
                _event("GOOGL", "2026-09-04", record="2026-09-07"),
                _event("TER", "2026-09-04", record="2026-09-04"),
                # 除息日在 7 天窗外；即使派发日在窗内，也不能算作本周事件。
                _event("WDC", "2026-09-08", pay="2026-09-02"),
                _event("PRED", "2026-09-02", forecast=True),
            ],
        }

        text = _card_text(cards.week_card(data, ""))

        self.assertIn("正式事项 **4** 个", text)
        for ticker in ("HD", "QCOM", "GOOGL", "TER"):
            self.assertEqual(1, text.count(f"**{ticker}**"))
        self.assertNotIn("**WDC**", text)
        self.assertNotIn("**PRED**", text)
        self.assertIn("另有 **1** 个预测", text)

    def test_upcoming_is_only_zero_to_fourteen_days(self):
        data = {
            "generated": "test",
            "pending": [
                {"ticker": "QCOM", "etype": "split", "date": "2026-09-03",
                 "days": 3, "products": []},
                {"ticker": "WDC", "etype": "split", "date": "2026-09-08",
                 "days": 8, "products": []},
                {"ticker": "LATE", "etype": "split", "date": "2026-09-20",
                 "days": 20, "products": []},
            ],
            "forecasts": [
                {"ticker": "HD", "etype": "dividend", "date": "2026-09-03",
                 "days": 3, "products": [], "srcs": ["Alpaca"], "forecast": True,
                 "references": [{"label": "SEC", "url": "https://example.invalid/hd"}]},
                {"ticker": "FAR", "etype": "dividend", "date": "2026-09-20",
                 "days": 20, "products": [], "srcs": ["Alpaca"], "forecast": True,
                 "references": [{"label": "SEC", "url": "https://example.invalid/far"}]},
            ],
        }

        text = _card_text(cards.upcoming_card(data, ""))

        self.assertIn("执行催办 **2** 个", text)
        self.assertIn("单源核验 **1** 个", text)
        self.assertIn("**QCOM**", text)
        self.assertIn("**WDC**", text)
        self.assertIn("**HD**", text)
        self.assertIn("单源待核实·勿执行", text)
        self.assertNotIn("**LATE**", text)
        self.assertNotIn("**FAR**", text)
        self.assertIn("与「本周（未来7天）」分开", text)


if __name__ == "__main__":
    unittest.main()
