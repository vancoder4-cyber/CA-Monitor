# -*- coding: utf-8 -*-
import copy
import datetime as dt
import importlib.util
import json
import os
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "bot"


class _Builder:
    """Enough of lark-oapi's builder surface to import bot.py in unit tests."""

    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: self


class _Client:
    @classmethod
    def builder(cls):
        return _Builder()


def _load_bot_module():
    lark = types.ModuleType("lark_oapi")
    lark.Client = _Client
    lark.EventDispatcherHandler = _Client
    lark.LARK_DOMAIN = "test"
    lark.ws = SimpleNamespace(Client=_Client)
    api = types.ModuleType("lark_oapi.api")
    im = types.ModuleType("lark_oapi.api.im")
    v1 = types.ModuleType("lark_oapi.api.im.v1")
    for name in (
        "CreateMessageRequest", "CreateMessageRequestBody", "CreateImageRequest",
        "CreateImageRequestBody", "P2ImMessageReceiveV1",
    ):
        setattr(v1, name, type(name, (), {"builder": classmethod(lambda cls: _Builder())}))
    fake_modules = {
        "lark_oapi": lark,
        "lark_oapi.api": api,
        "lark_oapi.api.im": im,
        "lark_oapi.api.im.v1": v1,
    }
    old_path = list(sys.path)
    try:
        sys.path.insert(0, str(BOT_DIR))
        with mock.patch.dict(os.environ, {
            "LARK_APP_ID": "test-app", "LARK_APP_SECRET": "test-secret",
        }), mock.patch.dict(sys.modules, fake_modules):
            spec = importlib.util.spec_from_file_location(
                "ca_monitor_bot_under_test", BOT_DIR / "bot.py"
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    finally:
        sys.path[:] = old_path


bot = _load_bot_module()
cards = bot.cards


def _snapshot():
    return {
        "schema_version": cards.PUBLIC_DATA_SCHEMA_VERSION,
        "generated": "2026-09-03 09:35 ET / 21:35 北京",
        "generated_at_utc": "2026-09-03T13:35:00Z",
        "valid_until_utc": "2026-09-04T23:59:00Z",
        "business_date": "2026-09-03",
        "source_sha": "test-sha",
        "coverage": [
            {"ticker": "AAPL", "spot": True, "contract": True, "monitored": True},
            {"ticker": "XAU", "spot": False, "contract": True, "monitored": False},
        ],
        "pending": [],
        "forecasts": [],
        "calendar": [],
        "announced": [],
        "recent_declares": [],
        "conflicts": [],
        "gaps": [],
        "changelog": [],
        "counts": {"pending": 0, "forecasts": 0, "conflicts": 0, "gaps": 0},
    }


class CommandRoutingTests(unittest.TestCase):
    def test_explicit_chinese_commands_do_not_require_a_space(self):
        cases = {
            "确认AAPL 0.26 2026-09-10 已比对公司公告": "confirm",
            "已核对AAPL 0.26 2026-09-10 最近更新完成": "confirm",
            "观察AAPL 2026-09-10 公司公告待出": "forecast",
            "需求提报增加公司公告提醒": "request",
            "留痕AAPL 最近更新": "audit",
            "查代码AAPL 本周": "lookup",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(expected, cards.parse_command(text))

    def test_ascii_aliases_require_real_word_boundaries(self):
        self.assertEqual("forecast", cards.parse_command("watch AAPL"))
        self.assertEqual("help", cards.parse_command("watchlist"))
        self.assertEqual("help", cards.parse_command("confirmation"))

    def test_all_fifteen_declared_commands_have_a_parse_route(self):
        self.assertEqual(15, len(cards.COMMANDS))
        for command in cards.COMMANDS:
            for keyword in command["kw"]:
                with self.subTest(command=command["key"], keyword=keyword):
                    self.assertEqual(command["key"], cards.parse_command(keyword))


class SnapshotValidationTests(unittest.TestCase):
    def test_current_contract_is_accepted(self):
        self.assertEqual("", cards.validate_snapshot(
            _snapshot(), today=dt.date(2026, 9, 3),
            now=dt.datetime(2026, 9, 3, 14, tzinfo=dt.timezone.utc),
        ))

    def test_old_contract_missing_field_and_stale_snapshot_are_rejected(self):
        old = _snapshot()
        old["schema_version"] -= 1
        self.assertIn("版本不一致", cards.validate_snapshot(
            old, today=dt.date(2026, 9, 3),
            now=dt.datetime(2026, 9, 3, 14, tzinfo=dt.timezone.utc),
        ))

        missing = _snapshot()
        missing.pop("recent_declares")
        self.assertIn("recent_declares", cards.validate_snapshot(
            missing, today=dt.date(2026, 9, 3),
            now=dt.datetime(2026, 9, 3, 14, tzinfo=dt.timezone.utc),
        ))

        stale = _snapshot()
        self.assertIn("已过期", cards.validate_snapshot(
            stale, today=dt.date(2026, 9, 7),
            now=dt.datetime(2026, 9, 4, 12, tzinfo=dt.timezone.utc),
        ))

        same_day_expired = _snapshot()
        same_day_expired["valid_until_utc"] = "2026-09-03T13:59:59Z"
        self.assertIn("超过有效时点", cards.validate_snapshot(
            same_day_expired, today=dt.date(2026, 9, 3),
            now=dt.datetime(2026, 9, 3, 14, tzinfo=dt.timezone.utc),
        ))

        future_generated = _snapshot()
        future_generated["generated_at_utc"] = "2026-09-03T14:11:00Z"
        self.assertIn("生成时间位于未来", cards.validate_snapshot(
            future_generated, today=dt.date(2026, 9, 3),
            now=dt.datetime(2026, 9, 3, 14, tzinfo=dt.timezone.utc),
        ))

        inverted = _snapshot()
        inverted["valid_until_utc"] = inverted["generated_at_utc"]
        self.assertIn("快照契约异常", cards.validate_snapshot(
            inverted, today=dt.date(2026, 9, 3),
            now=dt.datetime(2026, 9, 3, 14, tzinfo=dt.timezone.utc),
        ))

    def test_business_query_fails_visible_when_snapshot_is_unavailable(self):
        message = SimpleNamespace(
            message_id="fail-visible-1", chat_id="chat", chat_type="p2p", mentions=[],
            content=json.dumps({"text": "风险"}),
        )
        event = SimpleNamespace(
            message=message,
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_test")),
        )
        bot._seen.clear()
        with mock.patch.object(bot, "fetch_data", return_value={"_snapshot_error": "HTTP 503"}), \
                mock.patch.object(bot, "send_card") as send_card:
            bot.on_message(SimpleNamespace(event=event))
        send_card.assert_called_once()
        card = send_card.call_args.args[1]
        self.assertEqual("red", card["header"]["template"])
        self.assertIn("HTTP 503", json.dumps(card, ensure_ascii=False))
        self.assertIn("已停止输出", json.dumps(card, ensure_ascii=False))

    def test_help_remains_available_without_snapshot(self):
        message = SimpleNamespace(
            message_id="fail-visible-help", chat_id="chat", chat_type="p2p", mentions=[],
            content=json.dumps({"text": "帮助"}),
        )
        event = SimpleNamespace(
            message=message,
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_test")),
        )
        bot._seen.clear()
        with mock.patch.object(bot, "fetch_data", return_value={"_snapshot_error": "HTTP 503"}), \
                mock.patch.object(bot, "send_text") as send_text, \
                mock.patch.object(bot, "send_card") as send_card:
            bot.on_message(SimpleNamespace(event=event))
        send_text.assert_called_once()
        send_card.assert_not_called()

    def test_help_with_chinese_punctuation_remains_available_without_snapshot(self):
        message = SimpleNamespace(
            message_id="fail-visible-help-punctuation", chat_id="chat", chat_type="p2p", mentions=[],
            content=json.dumps({"text": "帮助：怎么使用"}),
        )
        event = SimpleNamespace(
            message=message,
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_test")),
        )
        bot._seen.clear()
        with mock.patch.object(bot, "fetch_data", return_value={"_snapshot_error": "HTTP 503"}), \
                mock.patch.object(bot, "send_text") as send_text, \
                mock.patch.object(bot, "send_card") as send_card:
            bot.on_message(SimpleNamespace(event=event))
        send_text.assert_called_once()
        send_card.assert_not_called()

    def test_audit_can_filter_historical_ticker_without_snapshot(self):
        message = SimpleNamespace(
            message_id="audit-without-snapshot", chat_id="chat", chat_type="p2p", mentions=[],
            content=json.dumps({"text": "留痕 V"}),
        )
        event = SimpleNamespace(
            message=message,
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_test")),
        )
        log = [
            {"ticker": "V", "etype": "dividend", "date": "2026-08-11", "action": "confirm"},
            {"ticker": "AAPL", "etype": "dividend", "date": "2026-08-11", "action": "confirm"},
        ]
        bot._seen.clear()
        with mock.patch.object(bot, "fetch_data", return_value={"_snapshot_error": "HTTP 503"}), \
                mock.patch.object(bot.ack, "get_ack_log", return_value=log), \
                mock.patch.object(bot.cards, "audit_card", return_value={"audit": True}) as audit_card, \
                mock.patch.object(bot, "send_card"):
            bot.on_message(SimpleNamespace(event=event))
        rows, _site_url, ticker = audit_card.call_args.args
        self.assertEqual("V", ticker)
        self.assertEqual(["V"], [row["ticker"] for row in rows])


class BotOverlayTests(unittest.TestCase):
    def test_apply_acks_updates_all_answer_surfaces_and_keeps_event_type_scope(self):
        data = _snapshot()
        matched = {"ticker": "AAPL", "etype": "dividend", "date": "2026-09-10"}
        other_type = {"ticker": "AAPL", "etype": "split", "date": "2026-09-10"}
        for field in ("pending", "calendar", "announced", "recent_declares"):
            data[field] = [dict(matched), dict(other_type)]
        data["conflicts"] = [dict(matched), dict(other_type)]
        data["gaps"] = [dict(matched), dict(other_type)]
        data["counts"].update({"conflicts": 2, "gaps": 2})

        with mock.patch.object(bot.ack, "get_acks", return_value=[dict(matched)]):
            result = bot.apply_acks(data)

        for field in ("pending", "calendar", "announced", "recent_declares"):
            self.assertTrue(result[field][0]["acked"], field)
            self.assertNotIn("acked", result[field][1], field)
        self.assertEqual([other_type], result["conflicts"])
        self.assertEqual([other_type], result["gaps"])
        self.assertEqual(1, result["counts"]["conflicts"])
        self.assertEqual(1, result["counts"]["gaps"])

    def test_future_manual_watch_is_immediately_visible(self):
        data = _snapshot()
        watch = {
            "ticker": "AAPL", "etype": "dividend", "date": "2026-09-10",
            "status": "watching", "note": "等待公司公告",
        }
        with mock.patch.object(bot.ack, "get_forecasts", return_value=[watch]):
            result = bot.apply_forecasts(data)

        self.assertEqual([], result["pending"])
        self.assertEqual(1, result["counts"]["forecasts"])
        event = result["forecasts"][0]
        self.assertEqual(7, event["days"])
        self.assertTrue(event["manual_watch"])
        self.assertTrue(event["watching"])
        self.assertEqual(["现货", "合约"], event["products"])
        self.assertEqual("review", event["contract_action"]["status"])

    def test_expired_malformed_and_unmonitored_watches_are_not_resurrected(self):
        data = _snapshot()
        watches = [
            {"ticker": "AAPL", "etype": "dividend", "date": "2026-09-02", "status": "watching"},
            {"ticker": "AAPL", "etype": None, "date": "2026-09-10", "status": "watching"},
            {"ticker": "AAPL", "etype": "dividend", "date": "not-a-date", "status": "watching"},
            {"ticker": "XAU", "etype": "dividend", "date": "2026-09-10", "status": "watching"},
        ]
        with mock.patch.object(bot.ack, "get_forecasts", return_value=watches):
            result = bot.apply_forecasts(data)
        self.assertEqual([], result["forecasts"])

    def test_promoted_or_shifted_published_event_suppresses_synthetic_duplicate(self):
        watch = {
            "ticker": "AAPL", "etype": "dividend", "date": "2026-09-10",
            "status": "watching", "note": "old prediction",
        }
        for published_field, published_event in (
            ("pending", {
                "ticker": "AAPL", "etype": "dividend", "date": "2026-09-10",
                "confirmed": True, "watching": True,
            }),
            ("forecasts", {
                "ticker": "AAPL", "etype": "dividend", "date": "2026-09-12",
                "confirmed": False, "watching": True,
            }),
        ):
            with self.subTest(published_field=published_field):
                data = _snapshot()
                data[published_field] = [copy.deepcopy(published_event)]
                with mock.patch.object(bot.ack, "get_forecasts", return_value=[watch]):
                    result = bot.apply_forecasts(data)
                self.assertEqual(
                    1,
                    len(result["pending"]) + len(result["forecasts"]),
                    result,
                )


if __name__ == "__main__":
    unittest.main()
