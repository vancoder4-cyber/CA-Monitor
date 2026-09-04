import datetime as dt
import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

import config as C
import contract_policy as CP
import notify_lark
import reconcile
import report
import run
import sources
import cards
import ack as bot_ack

# 本地测试环境可能未安装 Pillow；_label 是纯文本函数，用轻量占位模块即可加载验证。
try:
    import render as bot_render
except ModuleNotFoundError:
    sys.modules.pop("render", None)
    fake_pil = types.ModuleType("PIL")
    fake_pil.Image = types.SimpleNamespace()
    fake_pil.ImageDraw = types.SimpleNamespace()
    fake_pil.ImageFont = types.SimpleNamespace()
    with mock.patch.dict(sys.modules, {"PIL": fake_pil}):
        import render as bot_render


TODAY = dt.date(2026, 9, 1)
EVENT_DATE = "2026-09-10"
SIGNATURE = f"IBM|dividend|{EVENT_DATE}"
PRICE = {
    "value": 100.0,
    "date": "2026-08-31",
    "source": "Tiingo",
    "basis": "previous_session_unadjusted_close",
    "currency": "USD",
    "unit": "listed_security",
}


class ContractPolicyUnitTests(unittest.TestCase):
    def _evaluate(self, amount, **overrides):
        values = {
            "amount": amount,
            "subtype": "cash_dividend",
            "reference_price": PRICE,
            "amount_currency": "USD",
            "amount_unit": "listed_security",
            "value_verified": True,
            "today": TODAY,
        }
        values.update(overrides)
        with mock.patch.object(C, "CONTRACT_TICKERS", {"IBM"}), mock.patch.object(C, "SPOT_TICKERS", set()):
            return CP.evaluate("IBM", "dividend", **values)

    def test_cash_dividend_threshold_is_strictly_greater_than_three_percent(self):
        for amount in (2.99, 3.0):
            with self.subTest(amount=amount):
                decision = self._evaluate(amount)
                self.assertEqual("not_required", decision["status"])
                self.assertIn("合约：本次无需操作", decision["message"])
        decision = self._evaluate(3.0000004)
        self.assertEqual("required", decision["status"])
        self.assertIn("严格超过 3%", decision["message"])
        self.assertGreater(decision["impact_pct"], 3.0)

    def test_split_reverse_split_and_stock_dividend_use_same_three_percent_threshold(self):
        with mock.patch.object(C, "CONTRACT_TICKERS", {"IBM"}):
            for ratio, expected in (("2:1", "required"), ("1:10", "required"),
                                    ("100:97", "not_required"),
                                    ("103:100", "not_required"), ("1031:1000", "required")):
                with self.subTest(ratio=ratio, expected=expected):
                    decision = CP.evaluate(
                        "IBM", "split", ratio=ratio, subtype="split",
                        value_verified=True, today=TODAY,
                    )
                    self.assertEqual(expected, decision["status"])
            for rate, expected in ((3 / 97, "not_required"), (0.03, "not_required"),
                                   (0.031, "required"), (0.1, "required")):
                with self.subTest(rate=rate, expected=expected):
                    stock = CP.evaluate(
                        "IBM", "dividend", subtype="stock_dividend", amount=rate,
                        amount_unit="additional_share_per_share", value_verified=True, today=TODAY,
                    )
                    self.assertEqual(expected, stock["status"])
                    self.assertIn("送股", stock["message"])

    def test_missing_stale_or_unverified_input_never_becomes_no_action(self):
        cases = (
            {"reference_price": None},
            {"reference_price": {**PRICE, "date": "2026-08-01"}},
            {"reference_price": {**PRICE, "date": ""}},
            {"reference_price": {**PRICE, "date": "2026-09-02"}},
            {"amount_currency": "TWD"},
            {"amount_unit": "ordinary_share"},
            {"value_verified": False},
            {"disputed": True},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.assertEqual("review", self._evaluate(2.0, **overrides)["status"])

    def test_non_contract_is_not_applicable_and_follow_up_modes_are_product_aware(self):
        with mock.patch.object(C, "CONTRACT_TICKERS", {"IBM"}), mock.patch.object(C, "SPOT_TICKERS", set()):
            no_action = CP.evaluate(
                "IBM", "dividend", amount=2, subtype="cash_dividend",
                reference_price=PRICE, amount_currency="USD", amount_unit="listed_security",
                value_verified=True, today=TODAY,
            )
            required = CP.evaluate(
                "IBM", "dividend", amount=4, subtype="cash_dividend",
                reference_price=PRICE, amount_currency="USD", amount_unit="listed_security",
                value_verified=True, today=TODAY,
            )
            review = CP.evaluate(
                "IBM", "dividend", amount=2, subtype="cash_dividend",
                reference_price=None, value_verified=True, today=TODAY,
            )
            self.assertEqual("none", CP.follow_up_mode("IBM", no_action))
            self.assertEqual("execution", CP.follow_up_mode("IBM", required))
            self.assertEqual("verification", CP.follow_up_mode("IBM", review))
            self.assertEqual("contract-action", CP.reminder_state_suffix("IBM", required, "execution"))
            self.assertEqual("contract-review", CP.reminder_state_suffix("IBM", review, "verification"))

        with mock.patch.object(C, "CONTRACT_TICKERS", set()):
            self.assertEqual("not_applicable", CP.evaluate("AAPL", "dividend")["status"])

    def test_contract_review_reminder_is_verification_not_execution(self):
        fired = {}
        event = {
            "ticker": "IBM", "etype": "dividend", "date": EVENT_DATE, "days": 9,
            "forecast": False, "follow_up_mode": "verification",
            "reminder_state_suffix": "contract-review", "risk": ["合约：待核实"],
        }
        reminder = run.schedule_event_reminder(event, SIGNATURE, fired, TODAY.isoformat())
        self.assertTrue(reminder["verification"])
        self.assertIn("不是执行指令", reminder["risk_copy"])
        self.assertIn(f"{SIGNATURE}#contract-review", fired)

    def test_reference_price_prefers_newer_then_tiingo_on_same_day(self):
        sources.clear_reference_price("IBM")
        sources.record_reference_price("IBM", 99, "2026-08-29", "Tiingo")
        sources.record_reference_price("IBM", 101, "2026-08-31", "yfinance")
        sources.record_reference_price("IBM", 100, "2026-08-31", "Tiingo")
        self.assertEqual(PRICE, sources.reference_price("IBM"))
        sources.clear_reference_price("IBM")

    def test_reference_price_cache_keeps_only_complete_last_known_good_snapshots(self):
        sources.replace_reference_prices({"IBM": PRICE})
        sources.record_reference_price("IBM", float("nan"), "2026-09-01", "Tiingo")
        self.assertEqual(PRICE, sources.reference_price("IBM"))

        sources.replace_reference_prices({
            "IBM": {key: value for key, value in PRICE.items() if key != "unit"},
        })
        self.assertIsNone(sources.reference_price("IBM"))
        sources.clear_reference_price("IBM")

    def test_fractional_split_ratios_are_not_rounded_to_one_to_one(self):
        self.assertEqual("21:20", sources._ratio_from_float(1.05))
        self.assertEqual("11:10", sources._ratio_from_float(1.1))
        self.assertEqual("3:2", sources._ratio_from_float(1.5))
        self.assertEqual("2:3", sources._ratio_from_float(2 / 3))
        self.assertEqual("1:10", sources.normalize_ratio("1：10"))
        self.assertIsNone(sources.normalize_ratio("0.0000001"))
        self.assertIsNone(sources.normalize_ratio("1:10000000"))
        self.assertEqual(("1:10", "1：10"), bot_ack.parse_confirm_value("确认 IBM 1：10"))

    def test_split_ack_preserves_full_reverse_split_ratio(self):
        group = reconcile.EventGroup(
            ticker="IBM", etype="split", anchor_date=EVENT_DATE,
            by_source={
                "Alpaca": {"ex_date": EVENT_DATE, "ratio": "2:1", "subtype": "split"},
                "Tiingo": {"ex_date": EVENT_DATE, "ratio": "1:10", "subtype": "split"},
            },
            conflicts=["ratio: Alpaca=2:1, Tiingo=1:10"],
        )
        group.acked = True
        group.ack_exact = True
        group.ack_value = "1:10"
        with mock.patch.object(C, "CONTRACT_TICKERS", {"IBM"}), mock.patch.object(C, "SPOT_TICKERS", set()):
            decision = run.attach_product_action(group, None, TODAY, forecast=False)
        self.assertEqual("1:10", group.selected_ratio)
        self.assertEqual("required", decision["status"])
        self.assertEqual("1:10", group.value_display)

    def test_stock_dividend_and_adr_values_are_not_rendered_as_usd_cash(self):
        stock_display = CP.value_display(
            "dividend", amount=0.1, subtype="stock_dividend",
            amount_unit="additional_share_per_share",
        )
        twd_display = CP.value_display(
            "dividend", amount=2, subtype="cash_dividend",
            amount_currency="TWD", amount_unit="ordinary_share",
        )
        self.assertEqual("送股 0.1 股/股（10%）", stock_display)
        self.assertEqual("TWD 2/普通股", twd_display)
        fixture = {
            "ticker": "IBM", "etype": "dividend", "event_label": "送股",
            "amount": 0.1, "value_display": stock_display,
        }
        for rendered in (notify_lark._val(fixture), report._val_html(fixture), cards._val(fixture)):
            self.assertIn("送股 0.1 股/股", rendered)
            self.assertNotIn("$", rendered)
        self.assertIn("送股 0.1 股/股", bot_render._label({
            **fixture, "value_verified": True,
        }))
        self.assertIn("TWD 2/普通股", bot_render._label({
            "ticker": "TSM", "etype": "dividend", "event_label": "分红",
            "amount": 2, "value_display": twd_display, "value_verified": True,
        }))
        unverified = bot_render._label({
            "ticker": "IBM", "etype": "dividend", "amount": 2,
            "value_display": "$2", "value_verified": False,
        })
        self.assertIn("待核实", unverified)
        self.assertNotIn("$2", unverified)

    def test_yfinance_price_skips_latest_nan_and_uses_previous_valid_close(self):
        fake = mock.Mock()
        fake.history.return_value = {"Close": {
            dt.datetime(2026, 8, 28): 99.0,
            dt.datetime(2026, 8, 31): float("nan"),
        }}
        sources.clear_reference_price("IBM")
        with mock.patch.object(sources, "business_today", return_value=TODAY):
            sources._capture_yfinance_reference_price("IBM", fake)
        self.assertEqual(99.0, sources.reference_price("IBM")["value"])
        self.assertEqual("1mo", fake.history.call_args.kwargs["period"])
        sources.clear_reference_price("IBM")

    def test_filing_and_mixed_dividend_fail_safe(self):
        with mock.patch.object(C, "CONTRACT_TICKERS", {"IBM"}), mock.patch.object(C, "SPOT_TICKERS", set()):
            routine = CP.evaluate(
                "IBM", "filing", value_verified=True, filing_relevant=False, today=TODAY,
            )
            structural = CP.evaluate(
                "IBM", "filing", value_verified=True, filing_relevant=True, today=TODAY,
            )
            unknown = CP.evaluate(
                "IBM", "filing", value_verified=True, filing_relevant=None, today=TODAY,
            )
            mixed = CP.evaluate(
                "IBM", "dividend", amount=2, subtype="mixed_dividend",
                value_verified=True, today=TODAY,
            )
        self.assertEqual("not_required", routine["status"])
        self.assertEqual("none", CP.follow_up_mode("IBM", routine))
        self.assertEqual("review", structural["status"])
        self.assertEqual("review", unknown["status"])
        self.assertEqual("review", mixed["status"])

    def test_reconcile_preserves_unknown_structural_vs_explicit_routine_filing(self):
        unknown_event = sources.Event(
            "IBM", "filing", "Alpaca", ex_date=EVENT_DATE,
            note="other_corporate_action", raw={},
        )
        routine_event = sources.Event(
            "IBM", "filing", "SEC", ex_date=EVENT_DATE,
            note="8-K · 业绩与财务", raw={"relevant": False},
        )
        unknown_group = reconcile.reconcile_ticker([
            sources.SourceResult("Alpaca", "IBM", "ok", [unknown_event]),
        ])[0]
        routine_group = reconcile.reconcile_ticker([
            sources.SourceResult("SEC", "IBM", "ok", [routine_event]),
        ])[0]
        self.assertIsNone(unknown_group.by_source["Alpaca"]["relevant"])
        self.assertFalse(routine_group.by_source["SEC"]["relevant"])
        with mock.patch.object(C, "CONTRACT_TICKERS", {"IBM"}), mock.patch.object(C, "SPOT_TICKERS", set()):
            unknown_decision = run.attach_product_action(unknown_group, None, TODAY)
            routine_decision = run.attach_product_action(routine_group, None, TODAY)
        self.assertEqual("review", unknown_decision["status"])
        self.assertEqual("not_required", routine_decision["status"])

    def test_ack_matching_is_scoped_by_event_type_and_date(self):
        acks = [
            {"ticker": "IBM", "etype": "split", "date": EVENT_DATE, "value": "1:10"},
            {"ticker": "IBM", "date": EVENT_DATE, "value": "legacy-wide"},
        ]
        self.assertEqual("1:10", run._ack_match(acks, "IBM", "split", EVENT_DATE)["value"])
        self.assertIsNone(run._ack_match(acks, "IBM", "dividend", EVENT_DATE))

    def test_ack_matching_rejects_nonfinite_and_unrepresentable_values(self):
        for etype, value in (
            ("dividend", "NaN"), ("dividend", "Infinity"),
            ("dividend", "1e2"), ("dividend", "1_000"),
            ("dividend", "1e10000"), ("dividend", "1e-10000"),
            ("split", "0.0000001"), ("split", "1:10000000"),
        ):
            with self.subTest(etype=etype, value=value):
                stored = [{
                    "ticker": "IBM", "etype": etype, "date": EVENT_DATE,
                    "value": value,
                }]
                self.assertIsNone(run._ack_match(stored, "IBM", etype, EVENT_DATE))

    def test_empty_ack_or_date_only_official_source_cannot_bypass_value_gate(self):
        group = reconcile.EventGroup(
            ticker="IBM", etype="dividend", anchor_date=EVENT_DATE,
            by_source={
                "CompanyIR": {
                    "ex_date": EVENT_DATE,
                    "declaration_date": "2026-08-20",
                    "subtype": "cash_dividend",
                },
                "Alpaca": {
                    "ex_date": EVENT_DATE,
                    "amount": 2.0,
                    "subtype": "cash_dividend",
                    "amount_currency": "USD",
                    "amount_unit": "listed_security",
                },
            },
        )
        group.acked = True
        group.ack_exact = False
        group.ack_value = None
        with mock.patch.object(C, "CONTRACT_TICKERS", {"IBM"}), mock.patch.object(C, "SPOT_TICKERS", set()):
            decision = run.attach_product_action(group, PRICE, TODAY, forecast=False)
        self.assertEqual("review", decision["status"])
        self.assertIn("金额或比例尚未通过门禁", decision["message"])

    def test_exact_ack_inherits_unit_only_from_matching_source(self):
        group = reconcile.EventGroup(
            ticker="IBM", etype="dividend", anchor_date=EVENT_DATE,
            by_source={
                "CompanyIR": {
                    "ex_date": EVENT_DATE,
                    "amount": 2.0,
                    "subtype": "cash_dividend",
                    "amount_currency": "TWD",
                    "amount_unit": "ordinary_share",
                },
                "Alpaca": {
                    "ex_date": EVENT_DATE,
                    "amount": 1.5,
                    "subtype": "cash_dividend",
                    "amount_currency": "USD",
                    "amount_unit": "listed_security",
                },
            },
            conflicts=["amount: CompanyIR=2.0, Alpaca=1.5"],
        )
        group.acked = True
        group.ack_exact = True
        group.ack_value = 2.0
        with mock.patch.object(C, "CONTRACT_TICKERS", {"IBM"}), mock.patch.object(C, "SPOT_TICKERS", set()):
            decision = run.attach_product_action(group, PRICE, TODAY, forecast=False)
        self.assertEqual("review", decision["status"])
        self.assertIn("币种或证券单位不一致", decision["message"])


def _group(amount):
    fields = {
        "ex_date": EVENT_DATE,
        "declaration_date": "2026-08-20",
        "record_date": EVENT_DATE,
        "pay_date": "2026-09-20",
        "amount": amount,
        "subtype": "cash_dividend",
        "amount_currency": "USD",
        "amount_unit": "listed_security",
    }
    return {
        "ticker": "IBM",
        "etype": "dividend",
        "anchor_date": EVENT_DATE,
        "by_source": {"Alpaca": dict(fields), "Tiingo": dict(fields)},
        "sources_ok": ["Alpaca", "Tiingo"],
        "status": "confirmed",
        "conflicts": [],
        "gaps": [],
        "note": "",
    }


class ContractPolicyBuildTests(unittest.TestCase):
    def _build(self, amount, *, spot, prior_action_status=None, group_data=None,
               legacy_fired=False, acks=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cache = root / "cache"
        cache.mkdir()
        (cache / "IBM.json").write_text(json.dumps({
            "ticker": "IBM",
            "fetched": "2026-09-01T12:00:00",
            "health": {"Alpaca": "ok", "Tiingo": "ok"},
            "reference_price": PRICE,
            "groups": [group_data or _group(amount)],
        }), encoding="utf-8")
        state = root / "state.json"
        if prior_action_status or legacy_fired:
            state.write_text(json.dumps({
                "seen": {SIGNATURE: "2026-08-20"},
                "fired_rounds": {SIGNATURE: {"headsup": True}} if legacy_fired else {},
                "declared": {SIGNATURE: "2026-08-20"},
                "forecast_status": {},
                "contract_action_status": ({
                    SIGNATURE: {"status": prior_action_status, "last_seen": "2026-08-31"},
                } if prior_action_status else {}),
            }), encoding="utf-8")
        site_data = root / "site_data.json"
        patches = (
            mock.patch.object(run, "CACHE", str(cache)),
            mock.patch.object(run, "STATE_PATH", str(state)),
            mock.patch.object(run, "FORECAST_WATCH_PATH", str(root / "forecast_watch.json")),
            mock.patch.object(run, "OUT_HTML", str(root / "dashboard.html")),
            mock.patch.object(run, "OUT_DIGEST", str(root / "digest.txt")),
            mock.patch.object(run, "OUT_SITEDATA", str(site_data)),
            mock.patch.object(run, "business_today", return_value=TODAY),
            mock.patch.object(reconcile, "business_today", return_value=TODAY),
            mock.patch.object(run, "load_refs", return_value={"ir_dividend": {}}),
            mock.patch.object(run, "load_acknowledged", return_value=acks or []),
            mock.patch.object(run, "load_forecast_watches", return_value=[]),
            mock.patch.object(run.notify_lark, "notify", return_value=(False, "test skip")),
            mock.patch.object(C, "TICKERS", ["IBM"]),
            mock.patch.object(C, "ALL_ASSETS", ["IBM"]),
            mock.patch.object(C, "SPOT_TICKERS", {"IBM"} if spot else set()),
            mock.patch.object(C, "CONTRACT_TICKERS", {"IBM"}),
        )
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            with redirect_stdout(io.StringIO()):
                alerts = run.build()
        return (
            alerts,
            json.loads(site_data.read_text(encoding="utf-8")),
            json.loads(state.read_text(encoding="utf-8")),
            (root / "dashboard.html").read_text(encoding="utf-8"),
            (root / "digest.txt").read_text(encoding="utf-8"),
        )

    def test_contract_only_low_impact_is_reported_but_not_repeated_as_execution(self):
        alerts, data, state, page, digest = self._build(2.0, spot=False)
        self.assertEqual(1, len(alerts["pending"]))
        self.assertEqual([], alerts["rounds"])
        self.assertEqual("not_required", alerts["pending"][0]["contract_action"]["status"])
        self.assertNotIn("reference_price", alerts["pending"][0]["contract_action"])
        self.assertNotIn("reference_prices", data)
        self.assertIn("合约：本次无需操作", alerts["pending"][0]["risk"][0])
        self.assertEqual("none", data["calendar"][0]["follow_up_mode"])
        self.assertEqual({}, state["fired_rounds"])
        self.assertIn("合约：本次无需操作", page)
        self.assertIn("合约：本次无需操作", digest)

        lark = json.dumps(
            notify_lark._build_card(alerts, {"generated": "test"}), ensure_ascii=False,
        )
        self.assertIn("合约：本次无需操作", lark)
        self.assertNotIn("有正式临近催办事项", lark)

        for name, card in {
            "calendar": cards.calendar_card(data, ""),
            "announce": cards.announce_card(data, ""),
            "lookup": cards.lookup_card(data, "IBM", ""),
        }.items():
            self.assertIn(
                "合约：本次无需操作",
                json.dumps(card, ensure_ascii=False),
                name,
            )
        upcoming = json.dumps(cards.upcoming_card(data, ""), ensure_ascii=False)
        self.assertNotIn("**IBM**", upcoming)
        self.assertIn("合约无需操作的事项仍可在『日历/查代码』查看", upcoming)
        risk = json.dumps(cards.risk_card(data, ""), ensure_ascii=False)
        self.assertNotIn("**IBM**", risk)
        self.assertIn("当前无风控事项", risk)

        promoted = {**alerts["pending"][0], "kind": "promoted"}
        promoted_alerts = {
            "new": [], "rounds": [], "conflicts": [], "gaps": [], "pending": [],
            "announced": [], "forecasts": [], "forecast_updates": [promoted],
            "contract_updates": [], "review": {},
        }
        promoted_lark = json.dumps(
            notify_lark._build_card(promoted_alerts, {"generated": "test"}), ensure_ascii=False,
        )
        self.assertIn("合约：本次无需操作", promoted_lark)
        self.assertIn("合约：本次无需操作", report.build_text_digest(promoted_alerts, {"generated": "test"}))

    def test_build_keeps_conflict_and_gap_when_stored_ack_has_no_valid_value(self):
        group = _group(2.0)
        group["by_source"]["Tiingo"]["amount"] = 4.0
        group["conflicts"] = ["amount: Alpaca=2.0, Tiingo=4.0"]
        group["gaps"] = ["pay_date: missing"]
        group["status"] = "conflict"
        for invalid in (None, "", 0, "-1", "NaN"):
            with self.subTest(value=invalid):
                stored = [{
                    "ticker": "IBM", "etype": "dividend", "date": EVENT_DATE,
                    "value": invalid,
                }]
                alerts, data, _, _, _ = self._build(
                    2.0, spot=False, group_data=group, acks=stored,
                )
                self.assertEqual(1, len(alerts["conflicts"]))
                self.assertEqual(1, len(alerts["gaps"]))
                self.assertEqual([], alerts["resolved"])
                self.assertFalse(data["calendar"][0]["acked"])

    def test_build_never_injects_nonfinite_ack_into_clean_event_or_public_json(self):
        for invalid in ("NaN", "Infinity", "-1", 0):
            with self.subTest(value=invalid):
                stored = [{
                    "ticker": "IBM", "etype": "dividend", "date": EVENT_DATE,
                    "value": invalid,
                }]
                alerts, data, _, _, _ = self._build(2.0, spot=False, acks=stored)
                self.assertEqual(2.0, alerts["pending"][0]["amount"])
                self.assertEqual(2.0, data["calendar"][0]["amount"])
                # 严格 JSON 编码是 Pages/Bot 客户端的真实契约；NaN/Infinity 不可出现。
                json.dumps(data, allow_nan=False)

    def test_build_keeps_legacy_split_factors_but_publishes_canonical_ratios(self):
        group = {
            "ticker": "IBM", "etype": "split", "anchor_date": EVENT_DATE,
            "by_source": {
                "Alpaca": {"ex_date": EVENT_DATE, "ratio": "2:1", "subtype": "split"},
                "Tiingo": {"ex_date": EVENT_DATE, "ratio": "3:1", "subtype": "split"},
            },
            "sources_ok": ["Alpaca", "Tiingo"], "status": "conflict",
            "conflicts": ["ratio: Alpaca=2:1, Tiingo=3:1"], "gaps": [], "note": "",
        }
        for legacy_factor, expected in (("4", "4:1"), ("0.1", "1:10")):
            with self.subTest(legacy_factor=legacy_factor):
                stored = [{
                    "ticker": "IBM", "etype": "split", "date": EVENT_DATE,
                    "value": legacy_factor,
                }]
                alerts, data, _, _, _ = self._build(
                    None, spot=False, group_data=group, acks=stored,
                )
                self.assertEqual([], alerts["conflicts"])
                self.assertEqual(expected, alerts["resolved"][0]["value"])
                self.assertEqual(expected, alerts["pending"][0]["ratio"])
                self.assertEqual(expected, data["calendar"][0]["ratio"])
                self.assertEqual("required", alerts["pending"][0]["contract_action"]["status"])

    def test_contract_only_high_impact_enters_contract_action_cadence(self):
        alerts, data, state, _, _ = self._build(4.0, spot=False)
        self.assertEqual(1, len(alerts["rounds"]))
        self.assertFalse(alerts["rounds"][0]["verification"])
        self.assertEqual("required", alerts["rounds"][0]["contract_action"]["status"])
        self.assertIn(f"{SIGNATURE}#contract-action", state["fired_rounds"])
        self.assertIn("合约：需操作", json.dumps(cards.upcoming_card(data, ""), ensure_ascii=False))
        self.assertIn("**IBM**", json.dumps(cards.risk_card(data, ""), ensure_ascii=False))

    def test_spot_still_follows_up_when_contract_side_is_no_action(self):
        alerts, _, state, _, _ = self._build(2.0, spot=True)
        self.assertEqual(1, len(alerts["rounds"]))
        risks = alerts["rounds"][0]["risk"]
        self.assertTrue(any("现货" in risk for risk in risks))
        self.assertTrue(any("合约：本次无需操作" in risk for risk in risks))
        self.assertIn(SIGNATURE, state["fired_rounds"])

    def test_confirmed_event_with_value_conflict_is_formal_but_contract_review(self):
        group = _group(2.0)
        group["by_source"]["Tiingo"]["amount"] = 4.0
        group["conflicts"] = ["amount: Alpaca=2.0, Tiingo=4.0"]
        group["status"] = "conflict"
        alerts, data, _, _, _ = self._build(
            2.0, spot=False, group_data=group,
        )
        self.assertEqual([], alerts["forecasts"])
        self.assertEqual(1, len(alerts["pending"]))
        self.assertFalse(alerts["pending"][0]["forecast"])
        self.assertEqual("review", alerts["pending"][0]["contract_action"]["status"])
        self.assertEqual("verification", alerts["pending"][0]["follow_up_mode"])
        self.assertIn("合约：待核实", data["calendar"][0]["risk"][0])
        self.assertIsNone(data["pending"][0]["amount"])
        self.assertEqual("", data["pending"][0]["value_display"])
        self.assertEqual([], alerts["rounds"])
        for output in (
            data["pending"],
            report.build_text_digest(alerts, {"generated": "test"}),
            json.dumps(notify_lark._build_card(alerts, {"generated": "test"}), ensure_ascii=False),
        ):
            text = json.dumps(output, ensure_ascii=False) if not isinstance(output, str) else output
            self.assertNotIn("$2", text)
            self.assertNotIn("$4", text)

    def test_date_only_official_source_does_not_verify_single_vendor_amount(self):
        fields = _group(2.0)
        fields["by_source"] = {
            "CompanyIR": {
                "ex_date": EVENT_DATE,
                "declaration_date": "2026-08-20",
                "subtype": "cash_dividend",
            },
            "Alpaca": fields["by_source"]["Alpaca"],
        }
        fields["sources_ok"] = ["CompanyIR", "Alpaca"]
        alerts, data, _, page, digest = self._build(
            2.0, spot=False, group_data=fields,
        )
        self.assertFalse(alerts["pending"][0]["value_verified"])
        self.assertEqual("review", alerts["pending"][0]["contract_action"]["status"])
        operational_page = page.split('<div class="panel" id="panel-log">', 1)[0]
        surfaces = (
            operational_page,
            digest,
            json.dumps(notify_lark._build_card(alerts, {"generated": "test"}), ensure_ascii=False),
            json.dumps(cards.lookup_card(data, "IBM", ""), ensure_ascii=False),
        )
        for rendered in surfaces:
            self.assertIn("未交叉验证", rendered)
            self.assertNotIn("$2", rendered)

    def test_review_or_required_to_no_action_emits_one_resolution_update(self):
        for previous in ("review", "required"):
            with self.subTest(previous=previous):
                alerts, data, _, page, digest = self._build(
                    2.0, spot=False, prior_action_status=previous,
                )
                self.assertEqual([], alerts["rounds"])
                self.assertEqual(1, len(alerts["contract_updates"]))
                update = alerts["contract_updates"][0]
                self.assertEqual("not_required", update["current_status"])
                self.assertIn("合约：本次无需操作", update["risk"][0])
                self.assertEqual(1, len(data["contract_updates"]))
                self.assertIn("合约操作结论更新", page)
                self.assertIn("合约操作结论更新", digest)
                lark = json.dumps(
                    notify_lark._build_card(alerts, {"generated": "test"}), ensure_ascii=False,
                )
                self.assertIn("已确认合约本次无需操作", lark)

    def test_legacy_execution_reminder_gets_one_no_action_resolution(self):
        alerts, _, _, _, _ = self._build(
            2.0, spot=False, legacy_fired=True,
        )
        self.assertEqual(1, len(alerts["contract_updates"]))
        self.assertTrue(alerts["contract_updates"][0]["migration_resolution"])
        self.assertEqual("not_required", alerts["contract_updates"][0]["current_status"])


if __name__ == "__main__":
    unittest.main()
