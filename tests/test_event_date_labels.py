import datetime as dt
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
import notify_lark
import run
import report
from bot import cards


def _alerts():
    return {
        "new": [],
        "rounds": [],
        "conflicts": [],
        "gaps": [],
        "pending": [],
        "announced": [],
        "forecasts": [],
        "forecast_updates": [],
        "contract_updates": [],
        "review": {},
    }


def _event(ticker, etype, **extra):
    event = {
        "ticker": ticker,
        "etype": etype,
        "date": "2026-09-10",
        "days": 7,
        "decl": "2026-09-01",
        "products": ["现货"],
        "risk": [],
        "event_id": f"{ticker}|{etype}|2026-09-10",
    }
    event.update(extra)
    return event


def _text(card):
    return json.dumps(card, ensure_ascii=False)


class ConfigDateLabelTests(unittest.TestCase):
    def test_alert_copy_uses_typed_anchor_and_neutral_fallback(self):
        self.assertIn("距除息剩 7 天", config.alert_copy(7, "dividend"))
        self.assertIn("距生效剩 7 天", config.alert_copy(7, "split"))
        self.assertIn("距关键日剩 7 天", config.alert_copy(7))
        self.assertNotIn("除息", config.alert_copy(7, "split"))
        self.assertIn("今日除息", config.alert_copy(0, "dividend"))
        self.assertIn("今日生效", config.alert_copy(0, "split"))
        self.assertNotIn("剩 1 天", config.alert_copy(0, "split"))

    def test_producer_writes_typed_copy_into_new_payload(self):
        reminder = run.schedule_event_reminder(
            _event("IBM", "split"),
            "IBM|split|2026-09-10",
            {},
            "2026-09-03",
        )
        self.assertIn("距生效剩 7 天", reminder["ops"])
        self.assertNotIn("距除息", reminder["ops"])


class LarkDateLabelTests(unittest.TestCase):
    def test_new_announcements_use_event_specific_date_labels(self):
        alerts = _alerts()
        alerts["announced"] = [
            _event("AAPL", "dividend", amount=0.26, value_verified=True),
            _event("IBM", "split", ratio="1:10", value_verified=True),
        ]

        text = _text(notify_lark._build_card(alerts, {"generated": "test"}))

        self.assertIn("宣告 2026-09-01 · 除息 2026-09-10", text)
        self.assertIn("宣告 2026-09-01 · 生效 2026-09-10", text)

    def test_execution_reminders_rebuild_legacy_copy_with_typed_anchor(self):
        cases = (
            ("AAPL", "dividend", "距除息剩 7 天", "距生效剩 7 天"),
            ("IBM", "split", "距生效剩 7 天", "距除息剩 7 天"),
        )
        for ticker, etype, expected, forbidden in cases:
            with self.subTest(etype=etype):
                alerts = _alerts()
                # 模拟旧 payload 中已经写死为「除息」的催办文案。
                alerts["rounds"] = [
                    _event(ticker, etype, ops="⏱ 催办:距除息剩 7 天"),
                ]

                text = _text(notify_lark._build_card(alerts, {"generated": "test"}))

                self.assertIn(expected, text)
                self.assertNotIn(forbidden, text)

    def test_promoted_split_uses_effective_label(self):
        alerts = _alerts()
        alerts["forecast_updates"] = [
            _event("IBM", "split", kind="promoted", official=True),
        ]

        text = _text(notify_lark._build_card(alerts, {"generated": "test"}))

        self.assertIn("预测已转正式 — 生效 2026-09-10", text)
        self.assertNotIn("除息/生效 2026-09-10", text)


class BotDateLabelTests(unittest.TestCase):
    def test_announce_card_uses_event_specific_date_labels(self):
        data = {
            "recent_declares": [
                _event("AAPL", "dividend", amount=0.26, value_verified=True,
                       references=[{"label": "官方", "url": "https://example.invalid/aapl"}]),
                _event("IBM", "split", ratio="1:10", value_verified=True),
            ],
        }

        text = _text(cards.announce_card(data, ""))

        self.assertIn("宣告 2026-09-01 · 除息 2026-09-10", text)
        self.assertIn("宣告 2026-09-01 · 生效 2026-09-10", text)

    def test_upcoming_and_lookup_cadence_use_typed_anchor(self):
        dividend = _event(
            "AAPL", "dividend", amount=0.26, value_verified=True,
            references=[{"label": "官方", "url": "https://example.invalid/aapl"}],
        )
        split = _event("IBM", "split", ratio="1:10", value_verified=True)
        data = {
            "generated": "test",
            "business_date": "2026-09-03",
            "pending": [dividend, split],
            "forecasts": [],
            "calendar": [dividend, split],
            "coverage": [
                {"ticker": "IBM", "name": "IBM", "spot": True,
                 "contract": False, "monitored": True, "type_cn": "股票"},
            ],
            "refs": {},
        }

        upcoming = _text(cards.upcoming_card(data, ""))
        lookup = _text(cards.lookup_card(data, "IBM", ""))

        self.assertIn("距除息剩 7 天", upcoming)
        self.assertIn("距生效剩 7 天", upcoming)
        self.assertIn("距生效剩 7 天", lookup)
        self.assertNotIn("距除息剩 7 天", lookup)


class WebsiteCalendarDateLabelTests(unittest.TestCase):
    def test_split_calendar_pill_uses_effective_not_ex_rights_label(self):
        marks = {
            "2026-09-10": [{
                "tk": "IBM", "kind": "ex", "etype": "split",
                "status": "confirmed", "text": "1:10", "tip": "split",
                "url": "",
            }],
        }
        rendered = report._render_month(
            2026, 9, marks, dt.date(2026, 9, 3),
        )
        self.assertIn(">生效<", rendered)
        self.assertNotIn("除权·生效", rendered)


if __name__ == "__main__":
    unittest.main()
