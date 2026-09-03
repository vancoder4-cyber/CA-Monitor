import datetime as dt
import json
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as C
import notify_lark
import report
import run
from bot import cards

try:
    from bot import render as bot_render
except ModuleNotFoundError:
    sys.modules.pop("bot.render", None)
    fake_pil = types.ModuleType("PIL")
    fake_pil.Image = types.SimpleNamespace()
    fake_pil.ImageDraw = types.SimpleNamespace()
    fake_pil.ImageFont = types.SimpleNamespace()
    with mock.patch.dict(sys.modules, {"PIL": fake_pil}):
        from bot import render as bot_render


TODAY = dt.date(2026, 9, 3)
EVENT_DATE = "2026-09-10"
SEC_URL = "https://www.sec.gov/Archives/edgar/data/1/filing.htm"


def _alerts(round_event=None, pending_event=None):
    return {
        "new": [],
        "rounds": [round_event] if round_event else [],
        "conflicts": [],
        "gaps": [],
        "pending": [pending_event] if pending_event else [],
        "announced": [],
        "resolved": [],
        "forecasts": [],
        "forecast_updates": [],
        "contract_updates": [],
        "review": {
            "open": 0,
            "overdue": 0,
            "max_age": 0,
            "escalate_days": 3,
            "conflicts": 0,
            "gaps": 0,
        },
    }


def _event_from_group(group):
    return {
        "ticker": group.ticker,
        "etype": group.etype,
        "date": EVENT_DATE,
        "event_id": f"{group.ticker}|{group.etype}|{EVENT_DATE}",
        "days": 7,
        "status": "confirmed",
        "confirmed": True,
        "forecast": False,
        "products": C.product_tags(group.ticker),
        "srcs": sorted(group.by_source),
        "decl": None,
        "record": None,
        "pay": None,
        "note": group.note,
        "event_label": group.event_label,
        "value_display": group.value_display,
        "value_verified": group.value_verified,
        **run._product_fields(group),
    }


class VerificationKindTests(unittest.TestCase):
    def test_central_product_fields_distinguish_all_three_verification_kinds(self):
        filing = SimpleNamespace(
            ticker="TSM",
            etype="filing",
            note="6-K · 疑似公司行动（分红，待核实）",
            by_source={"SEC": {"relevant": None, "url": SEC_URL}},
            conflicts=[],
        )
        contract = SimpleNamespace(
            ticker="IBM",
            etype="dividend",
            note="",
            by_source={"CompanyIR": {
                "amount": 2.0,
                "amount_currency": "USD",
                "amount_unit": "listed_security",
            }},
            conflicts=[],
        )
        forecast = SimpleNamespace(
            ticker="IBM",
            etype="dividend",
            note="",
            by_source={"Alpaca": {
                "amount": 2.0,
                "amount_currency": "USD",
                "amount_unit": "listed_security",
            }},
            conflicts=[],
        )
        with (
            mock.patch.object(C, "SPOT_TICKERS", set()),
            mock.patch.object(C, "CONTRACT_TICKERS", {"TSM", "IBM"}),
        ):
            run.attach_product_action(filing, None, TODAY, forecast=False)
            run.attach_product_action(contract, None, TODAY, forecast=False)
            run.attach_product_action(forecast, None, TODAY, forecast=True)

        self.assertEqual("filing_terms", filing.verification_kind)
        self.assertEqual("contract_threshold", contract.verification_kind)
        self.assertEqual("forecast", forecast.verification_kind)
        for group in (filing, contract, forecast):
            self.assertEqual(
                group.verification_kind,
                run._product_fields(group)["verification_kind"],
            )

    def test_filing_terms_and_contract_threshold_use_distinct_copy_on_every_surface(self):
        filing = SimpleNamespace(
            ticker="TSM",
            etype="filing",
            note="6-K · 疑似公司行动（分红，待核实）",
            by_source={"SEC": {"relevant": None, "url": SEC_URL}},
            conflicts=[],
        )
        contract = SimpleNamespace(
            ticker="IBM",
            etype="dividend",
            note="",
            by_source={"CompanyIR": {
                "amount": 2.0,
                "amount_currency": "USD",
                "amount_unit": "listed_security",
            }},
            conflicts=[],
        )
        with (
            mock.patch.object(C, "SPOT_TICKERS", set()),
            mock.patch.object(C, "CONTRACT_TICKERS", {"TSM", "IBM"}),
        ):
            run.attach_product_action(filing, None, TODAY, forecast=False)
            run.attach_product_action(contract, None, TODAY, forecast=False)
            filing_event = _event_from_group(filing)
            contract_event = _event_from_group(contract)

        filing_round = run.schedule_event_reminder(
            filing_event,
            filing_event["event_id"],
            {},
            TODAY.isoformat(),
        )
        contract_round = run.schedule_event_reminder(
            contract_event,
            contract_event["event_id"],
            {},
            TODAY.isoformat(),
        )
        self.assertEqual("filing_terms", filing_round["verification_kind"])
        self.assertEqual("contract_threshold", contract_round["verification_kind"])
        self.assertIn("公司行动条款核验", filing_round["ops"])
        self.assertIn("核实前勿执行", filing_round["ops"])
        self.assertNotIn("合约门槛", filing_round["ops"])
        self.assertIn("合约门槛待核实", contract_round["ops"])

        data = {
            "generated": "test",
            "business_date": TODAY.isoformat(),
            "pending": [filing_event, contract_event],
            "forecasts": [],
            "calendar": [filing_event, contract_event],
            "refs": {},
            "coverage": [
                {"ticker": "TSM", "name": "TSM", "spot": False, "contract": True,
                 "monitored": True, "type_cn": "ADR"},
                {"ticker": "IBM", "name": "IBM", "spot": False, "contract": True,
                 "monitored": True, "type_cn": "个股"},
            ],
        }
        bot_upcoming = json.dumps(cards.upcoming_card(data, ""), ensure_ascii=False)
        bot_lookup = json.dumps(cards.lookup_card(data, "TSM", ""), ensure_ascii=False)
        lark = json.dumps(
            notify_lark._build_card(
                _alerts(round_event=filing_round),
                {"generated": "test"},
            ),
            ensure_ascii=False,
        )
        digest = report.build_text_digest(
            _alerts(round_event=filing_round, pending_event=filing_event),
            {"generated": "test"},
        )
        with mock.patch.object(C, "TICKERS", []), mock.patch.object(C, "ALL_ASSETS", []):
            dashboard = report.build_dashboard(
                {},
                {},
                _alerts(pending_event=filing_event),
                {"generated": "test", "business_date": TODAY.isoformat()},
            )

        self.assertIn("公司行动条款核验 · [合约] **TSM**", bot_upcoming)
        self.assertNotIn("合约门槛核验 · [合约] **TSM**", bot_upcoming)
        self.assertIn("公司行动条款核验", bot_lookup)
        self.assertIn("核实前勿执行", bot_lookup)
        self.assertIn(SEC_URL, bot_upcoming)
        self.assertIn(SEC_URL, bot_lookup)
        self.assertIn("公司行动条款核验 · [合约] **TSM**", lark)
        self.assertNotIn("合约门槛核验 · [合约] **TSM**", lark)
        self.assertIn(SEC_URL, lark)
        self.assertIn("[公司行动条款核验·勿执行] [合约] TSM", digest)
        self.assertNotIn("[合约门槛核验·勿执行] [合约] TSM", digest)
        self.assertIn(SEC_URL, digest)
        self.assertIn("公司行动条款核验", dashboard)
        self.assertIn("核实前勿执行", dashboard)
        self.assertIn(SEC_URL, dashboard)

        contract_data = {**data, "pending": [contract_event], "calendar": [contract_event]}
        contract_bot = json.dumps(cards.upcoming_card(contract_data, ""), ensure_ascii=False)
        contract_lark = json.dumps(
            notify_lark._build_card(
                _alerts(round_event=contract_round),
                {"generated": "test"},
            ),
            ensure_ascii=False,
        )
        contract_digest = report.build_text_digest(
            _alerts(round_event=contract_round, pending_event=contract_event),
            {"generated": "test"},
        )
        self.assertIn("合约门槛核验 · [合约] **IBM**", contract_bot)
        self.assertIn("合约门槛核验 · [合约] **IBM**", contract_lark)
        self.assertIn("[合约门槛核验·勿执行] [合约] IBM", contract_digest)

        self.assertIn("条款核验", bot_render._label(filing_event))
        self.assertIn("合约门槛核验", bot_render._label(contract_event))

    def test_suspected_filing_is_not_counted_or_styled_as_confirmed(self):
        filing = SimpleNamespace(
            ticker="TSM", etype="filing", anchor_date=TODAY.isoformat(),
            is_future=True, days_to=0, status="confirmed", forecast=False,
            note="6-K · 疑似公司行动（分红，待核实）",
            by_source={"SEC": {"relevant": None, "url": SEC_URL}},
            conflicts=[], gaps=[],
        )
        with (
            mock.patch.object(C, "SPOT_TICKERS", {"TSM"}),
            mock.patch.object(C, "CONTRACT_TICKERS", {"TSM"}),
        ):
            run.attach_product_action(filing, None, TODAY, forecast=False)
            event = _event_from_group(filing)
        event.update({"date": TODAY.isoformat(), "days": 0, "url": SEC_URL})
        data = {
            "generated": "test", "business_date": TODAY.isoformat(),
            "calendar": [event], "pending": [event], "forecasts": [],
            "conflicts": [], "refs": {},
            "coverage": [{"ticker": "TSM", "name": "TSM", "spot": True,
                          "contract": True, "monitored": True, "type_cn": "ADR"}],
        }

        today_text = json.dumps(cards.today_card(data, ""), ensure_ascii=False)
        week_text = json.dumps(cards.week_card(data, ""), ensure_ascii=False)
        risk_text = json.dumps(cards.risk_card(data, ""), ensure_ascii=False)
        lookup_text = json.dumps(cards.lookup_card(data, "TSM", ""), ensure_ascii=False)
        for text in (today_text, week_text):
            self.assertIn("正式事项 **0** 个", text)
            self.assertIn("疑似事项进入公司行动条款核验", text)
            self.assertIn("公司行动条款核验", text)
            self.assertIn("核实前勿执行", text)
            self.assertIn(SEC_URL, text)
        self.assertIn("已确认结构事项 **0**", risk_text)
        self.assertIn("条款核验 **1**", risk_text)
        self.assertIn("疑似公司行动(条款核验·勿执行)", risk_text)
        self.assertIn("疑似公司行动(条款核验·勿执行)", lookup_text)
        self.assertNotIn("已确认结构性行动(并购/退市/分拆/要约)", lookup_text)

        with mock.patch.object(C, "TICKERS", []), mock.patch.object(C, "ALL_ASSETS", []):
            dashboard = report.build_dashboard(
                {"TSM": [filing]}, {}, _alerts(pending_event=event),
                {"generated": "test", "business_date": TODAY.isoformat()},
            )
        self.assertIn("疑似相关·待核实", dashboard)
        self.assertIn(SEC_URL, dashboard)


if __name__ == "__main__":
    unittest.main()
