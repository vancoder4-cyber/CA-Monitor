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
        "filing_updates": [],
        "changelog": [],
        "counts": {"pending": 0, "forecasts": 0, "conflicts": 0, "gaps": 0},
    }


class CommandRoutingTests(unittest.TestCase):
    def test_explicit_chinese_commands_do_not_require_a_space(self):
        cases = {
            "确认AAPL 0.26 2026-09-10 已比对公司公告": "confirm",
            "确认备案AAPL|filing|2026-09-03|0123456789ab": "filing_resolve",
            "排除备案AAPL|filing|2026-09-03|0123456789ab": "filing_resolve",
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

    def test_all_sixteen_declared_commands_have_a_parse_route(self):
        self.assertEqual(16, len(cards.COMMANDS))
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

        with mock.patch.object(
                bot.ack, "get_acks", return_value=[{**matched, "value": "0.26"}]):
            result = bot.apply_acks(data)

        for field in ("pending", "calendar", "announced", "recent_declares"):
            self.assertTrue(result[field][0]["acked"], field)
            self.assertNotIn("acked", result[field][1], field)
        self.assertEqual([other_type], result["conflicts"])
        self.assertEqual([other_type], result["gaps"])
        self.assertEqual(1, result["counts"]["conflicts"])
        self.assertEqual(1, result["counts"]["gaps"])

    def test_invalid_ack_never_marks_event_or_hides_conflict_and_gap(self):
        for invalid in (None, "", "0", "-1", "NaN"):
            with self.subTest(value=invalid):
                data = _snapshot()
                event = {"ticker": "AAPL", "etype": "dividend", "date": "2026-09-10"}
                data["pending"] = [dict(event)]
                data["conflicts"] = [dict(event)]
                data["gaps"] = [dict(event)]
                data["counts"].update({"conflicts": 1, "gaps": 1})
                stored = [{**event, "value": invalid}]

                with mock.patch.object(bot.ack, "get_acks", return_value=stored):
                    result = bot.apply_acks(data)

                self.assertNotIn("acked", result["pending"][0])
                self.assertEqual([event], result["conflicts"])
                self.assertEqual([event], result["gaps"])
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


class FilingResolutionCommandTests(unittest.TestCase):
    EVENT_A = "AAPL|filing|2026-09-03|0123456789ab"
    EVENT_B = "AAPL|filing|2026-09-03|abcdef012345"

    @staticmethod
    def _candidate(event_id):
        return {
            "ticker": "AAPL", "etype": "filing", "date": "2026-09-03",
            "event_id": event_id, "filing_relevant": None,
            "verification_kind": "filing_terms", "follow_up_mode": "verification",
            "src_url": f"https://www.sec.gov/Archives/{event_id[-12:]}.htm",
        }

    def test_same_day_candidates_require_exact_id_and_exact_id_selects_one(self):
        data = _snapshot()
        data["pending"] = [self._candidate(self.EVENT_A), self._candidate(self.EVENT_B)]

        target, error = bot.filing_resolution_target(
            "排除备案 AAPL 2026-09-03", data, "AAPL"
        )
        self.assertIsNone(target)
        self.assertIn("同日有多份", error)
        self.assertIn(self.EVENT_A, error)
        self.assertIn(self.EVENT_B, error)

        target, error = bot.filing_resolution_target(
            f"排除备案 {self.EVENT_B}", data, "AAPL"
        )
        self.assertEqual("", error)
        self.assertEqual(self.EVENT_B, target["event_id"])

    def test_overlong_fingerprint_is_not_silently_truncated_to_a_valid_id(self):
        data = _snapshot()
        data["pending"] = [self._candidate(self.EVENT_A)]

        target, error = bot.filing_resolution_target(
            f"排除备案 {self.EVENT_A}f", data, "AAPL"
        )

        self.assertIsNone(target)
        self.assertIn("完整 event_id", error)

    def test_conflicting_resolution_words_fail_closed(self):
        self.assertEqual(
            "",
            bot.filing_resolution_status(
                f"确认备案 {self.EVENT_A}，但按普通备案无需操作"
            ),
        )

    def test_message_dispatch_writes_the_exact_id(self):
        data = _snapshot()
        candidate = self._candidate(self.EVENT_A)
        data["pending"] = [candidate]
        message = SimpleNamespace(
            message_id="filing-resolution-exact", chat_id="chat", chat_type="p2p",
            mentions=[], content=json.dumps({"text": f"排除备案 {self.EVENT_A} 已核对原文"}),
        )
        event = SimpleNamespace(
            message=message,
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_test")),
        )
        bot._seen.clear()
        with mock.patch.dict(os.environ, {"LARK_WRITE_ALLOWED_OPEN_IDS": "ou_test"}), \
                mock.patch.object(bot, "fetch_data", return_value=data), \
                mock.patch.object(bot.cards, "validate_snapshot", return_value=""), \
                mock.patch.object(bot.ack, "resolve_filing_review", return_value=(True, "ok")) as resolve, \
                mock.patch.object(bot, "send_card") as send_card:
            bot.on_message(SimpleNamespace(event=event))

        resolve.assert_called_once()
        args, kwargs = resolve.call_args
        self.assertEqual((self.EVENT_A, "routine"), args)
        self.assertEqual("AAPL", kwargs["ticker"])
        self.assertEqual("2026-09-03", kwargs["date"])
        self.assertNotIn("by", kwargs)
        self.assertNotIn("by_name", kwargs)
        self.assertNotIn("note", kwargs)
        self.assertEqual(candidate["src_url"], kwargs["src_url"])
        send_card.assert_called_once()


class WriteAuthorizationTests(unittest.TestCase):
    WRITE_TEXTS = (
        "排除备案 AAPL|filing|2026-09-03|0123456789ab",
        "确认 AAPL 0.26 2026-09-10",
        "观察 AAPL 2026-09-10",
        "需求 增加一个测试功能",
    )

    @staticmethod
    def _incoming(text, sender="ou_test"):
        message = SimpleNamespace(
            message_id=f"rbac-{hash((text, sender))}", chat_id="chat", chat_type="p2p",
            mentions=[], content=json.dumps({"text": text}),
        )
        return SimpleNamespace(
            event=SimpleNamespace(
                message=message,
                sender=SimpleNamespace(sender_id=SimpleNamespace(open_id=sender)),
            )
        )

    def test_empty_or_mismatched_allowlist_blocks_every_write_before_fetch(self):
        for allowlist in ("", "ou_someone_else"):
            for text in self.WRITE_TEXTS:
                with self.subTest(allowlist=allowlist or "<empty>", text=text):
                    bot._seen.clear()
                    with mock.patch.dict(
                            os.environ, {"LARK_WRITE_ALLOWED_OPEN_IDS": allowlist}), \
                            mock.patch.object(bot, "fetch_data") as fetch_data, \
                            mock.patch.object(bot.ack, "resolve_filing_review") as resolve, \
                            mock.patch.object(bot.ack, "add_ack") as add_ack, \
                            mock.patch.object(bot.ack, "add_forecast") as add_forecast, \
                            mock.patch.object(bot.ack, "add_request") as add_request, \
                            mock.patch.object(bot, "send_card") as send_card:
                        bot.on_message(self._incoming(text))

                    fetch_data.assert_not_called()
                    resolve.assert_not_called()
                    add_ack.assert_not_called()
                    add_forecast.assert_not_called()
                    add_request.assert_not_called()
                    send_card.assert_called_once()
                    rendered = json.dumps(send_card.call_args.args[1], ensure_ascii=False)
                    self.assertIn("写操作未授权", rendered)
                    self.assertIn("本次未修改任何状态", rendered)
                    self.assertNotIn("ou_someone_else", rendered)

    def test_allowlist_parser_requires_an_exact_open_id(self):
        with mock.patch.dict(os.environ, {
            "LARK_WRITE_ALLOWED_OPEN_IDS": " ou_first,ou_test, ou_third ",
        }):
            self.assertTrue(bot.write_authorized("ou_test"))
            self.assertFalse(bot.write_authorized("ou_tes"))
            self.assertFalse(bot.write_authorized(""))
            self.assertFalse(bot.write_authorized(None))

    def test_missing_sender_blocks_every_write_before_fetch(self):
        for text in self.WRITE_TEXTS:
            with self.subTest(text=text):
                message = SimpleNamespace(
                    message_id=f"rbac-missing-{hash(text)}", chat_id="chat",
                    chat_type="p2p", mentions=[], content=json.dumps({"text": text}),
                )
                incoming = SimpleNamespace(event=SimpleNamespace(message=message))
                bot._seen.clear()
                with mock.patch.dict(
                        os.environ, {"LARK_WRITE_ALLOWED_OPEN_IDS": "ou_test"}), \
                        mock.patch.object(bot, "fetch_data") as fetch_data, \
                        mock.patch.object(bot.ack, "resolve_filing_review") as resolve, \
                        mock.patch.object(bot.ack, "add_ack") as add_ack, \
                        mock.patch.object(bot.ack, "add_forecast") as add_forecast, \
                        mock.patch.object(bot.ack, "add_request") as add_request, \
                        mock.patch.object(bot, "send_card") as send_card:
                    bot.on_message(incoming)

                fetch_data.assert_not_called()
                resolve.assert_not_called()
                add_ack.assert_not_called()
                add_forecast.assert_not_called()
                add_request.assert_not_called()
                self.assertIn(
                    "写操作未授权",
                    json.dumps(send_card.call_args.args[1], ensure_ascii=False),
                )

    def test_authorized_operator_reaches_each_write_handler(self):
        data = _snapshot()
        data["pending"] = [
            FilingResolutionCommandTests._candidate(
                FilingResolutionCommandTests.EVENT_A
            ),
            {"ticker": "AAPL", "etype": "dividend", "date": "2026-09-10"},
        ]
        cases = (
            (f"排除备案 {FilingResolutionCommandTests.EVENT_A}", "resolve_filing_review"),
            ("确认 AAPL 0.26 2026-09-10", "add_ack"),
            ("观察 AAPL 2026-09-10", "add_forecast"),
            ("需求 增加一个测试功能", "add_request"),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                bot._seen.clear()
                with mock.patch.dict(
                        os.environ, {"LARK_WRITE_ALLOWED_OPEN_IDS": "ou_test"}), \
                        mock.patch.object(bot, "fetch_data", return_value=copy.deepcopy(data)), \
                        mock.patch.object(bot.cards, "validate_snapshot", return_value=""), \
                        mock.patch.object(bot, "get_user_name", return_value="Operator"), \
                        mock.patch.object(
                            bot.ack, "resolve_filing_review", return_value=(True, "ok")
                        ) as resolve, \
                        mock.patch.object(bot.ack, "add_ack", return_value=(True, "ok")) as add_ack, \
                        mock.patch.object(
                            bot.ack, "add_forecast", return_value=(True, "ok")
                        ) as add_forecast, \
                        mock.patch.object(
                            bot.ack, "add_request", return_value=(True, "ok")
                        ) as add_request, \
                        mock.patch.object(bot, "send_card"):
                    bot.on_message(self._incoming(text))

                handlers = {
                    "resolve_filing_review": resolve,
                    "add_ack": add_ack,
                    "add_forecast": add_forecast,
                    "add_request": add_request,
                }
                handlers.pop(expected).assert_called_once()
                for handler in handlers.values():
                    handler.assert_not_called()


class ConfirmValueCommandTests(unittest.TestCase):
    @staticmethod
    def _incoming(text):
        message = SimpleNamespace(
            message_id=f"confirm-value-{hash(text)}", chat_id="chat", chat_type="p2p",
            mentions=[], content=json.dumps({"text": text}),
        )
        return SimpleNamespace(event=SimpleNamespace(
            message=message,
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_test")),
        ))

    def test_on_message_rejects_missing_or_invalid_value_without_writeback(self):
        data = _snapshot()
        data["pending"] = [
            {"ticker": "AAPL", "etype": "dividend", "date": "2026-09-10"},
        ]
        for text in (
            "确认 AAPL 2026-09-10",
            "确认 AAPL 2026-09-10 已比对公司 8-K",
            "确认 AAPL 0 2026-09-10",
            "确认 AAPL -0.26 2026-09-10",
            "确认 AAPL 1e2 2026-09-10",
            "确认 AAPL 1,000 2026-09-10",
        ):
            with self.subTest(text=text):
                bot._seen.clear()
                with mock.patch.dict(os.environ, {"LARK_WRITE_ALLOWED_OPEN_IDS": "ou_test"}), \
                        mock.patch.object(bot, "fetch_data", return_value=copy.deepcopy(data)), \
                        mock.patch.object(bot.cards, "validate_snapshot", return_value=""), \
                        mock.patch.object(bot.ack, "add_ack") as add_ack, \
                        mock.patch.object(bot, "send_card") as send_card:
                    bot.on_message(self._incoming(text))

                add_ack.assert_not_called()
                rendered = json.dumps(send_card.call_args.args[1], ensure_ascii=False)
                self.assertIn("确认未成功", rendered)
                self.assertIn("不会写入", rendered)
                self.assertIn("不会消除冲突/空缺", rendered)

        split_data = _snapshot()
        split_data["pending"] = [
            {"ticker": "AAPL", "etype": "split", "date": "2026-09-10"},
        ]
        for text in ("确认 AAPL 4 2026-09-10", "确认 AAPL 1-10 2026-09-10",
                     "确认 AAPL 1 for 10 2026-09-10",
                     "确认 AAPL $1:10 2026-09-10",
                     "确认 AAPL USD 1:10 2026-09-10"):
            with self.subTest(text=text):
                bot._seen.clear()
                with mock.patch.dict(os.environ, {"LARK_WRITE_ALLOWED_OPEN_IDS": "ou_test"}), \
                        mock.patch.object(bot, "fetch_data", return_value=split_data), \
                        mock.patch.object(bot.cards, "validate_snapshot", return_value=""), \
                        mock.patch.object(bot.ack, "add_ack") as add_ack, \
                        mock.patch.object(bot, "send_card") as send_card:
                    bot.on_message(self._incoming(text))
                add_ack.assert_not_called()
                self.assertIn(
                    "新股数:旧股数",
                    json.dumps(send_card.call_args.args[1], ensure_ascii=False),
                )

    def test_on_message_preserves_full_reverse_split_ratio(self):
        data = _snapshot()
        data["pending"] = [
            {"ticker": "AAPL", "etype": "split", "date": "2026-09-10"},
        ]
        bot._seen.clear()
        with mock.patch.dict(os.environ, {"LARK_WRITE_ALLOWED_OPEN_IDS": "ou_test"}), \
                mock.patch.object(bot, "fetch_data", return_value=data), \
                mock.patch.object(bot.cards, "validate_snapshot", return_value=""), \
                mock.patch.object(bot, "get_user_name", return_value="Operator"), \
                mock.patch.object(bot.ack, "add_ack", return_value=(True, "ok")) as add_ack, \
                mock.patch.object(bot, "send_card"):
            bot.on_message(self._incoming("确认 AAPL 1:10 2026-09-10 已核对公告"))

        self.assertEqual("1:10", add_ack.call_args.args[1])
        self.assertEqual("split", add_ack.call_args.args[2])
        self.assertEqual("2026-09-10", add_ack.call_args.args[3])


if __name__ == "__main__":
    unittest.main()
