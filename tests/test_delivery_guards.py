import os
import sys
import json
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import notify_lark
import reconcile as R
import report
import run


def _alerts(with_content=False):
    return {
        "new": [],
        "rounds": ([{"ticker": "AAPL", "etype": "dividend", "date": "2030-01-01", "days": 7}]
                   if with_content else []),
        "conflicts": [],
        "gaps": [],
        "pending": [],
        "announced": [],
        "forecast_updates": [],
    }


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = {"code": 0} if payload is None else payload
        self.text = text or str(self._payload)
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload


class _InvalidJsonResponse(_Response):
    def json(self):
        raise ValueError("invalid json")


class LarkDeliveryTests(unittest.TestCase):
    def _env(self, **overrides):
        values = {
            "LARK_WEBHOOK": "https://example.invalid/hook",
            "LARK_SECRET": "",
            "LARK_DASHBOARD_URL": "",
            "LARK_NOTIFY_EMPTY": "0",
            "LARK_REQUIRED": "1",
        }
        values.update(overrides)
        return mock.patch.dict(os.environ, values, clear=False)

    def test_required_webhook_must_exist(self):
        with self._env(LARK_WEBHOOK=""):
            with self.assertRaises(notify_lark.LarkDeliveryError):
                notify_lark.notify(_alerts(), {"generated": "test"})

    def test_local_missing_webhook_is_a_legal_skip(self):
        with self._env(LARK_WEBHOOK="", LARK_REQUIRED="0"):
            with mock.patch.object(notify_lark.requests, "post") as post:
                sent, info = notify_lark.notify(_alerts(), {"generated": "test"})
        self.assertFalse(sent)
        self.assertIn("跳过推送", info)
        post.assert_not_called()

    def test_empty_alerts_are_a_legal_skip(self):
        with self._env():
            with mock.patch.object(notify_lark.requests, "post") as post:
                sent, info = notify_lark.notify(_alerts(), {"generated": "test"})
        self.assertFalse(sent)
        self.assertIn("无预警内容", info)
        post.assert_not_called()

    def test_pending_without_due_round_stays_quiet(self):
        alerts = _alerts()
        alerts["pending"] = [{"ticker": "AAPL", "etype": "dividend", "date": "2030-01-01", "days": 20}]
        with self._env():
            with mock.patch.object(notify_lark.requests, "post") as post:
                sent, info = notify_lark.notify(alerts, {"generated": "test"})
        self.assertFalse(sent)
        self.assertIn("无预警内容", info)
        post.assert_not_called()

    def test_network_failure_raises(self):
        with self._env():
            with mock.patch.object(
                notify_lark.requests,
                "post",
                side_effect=notify_lark.requests.RequestException("offline"),
            ):
                with self.assertRaises(notify_lark.LarkDeliveryError):
                    notify_lark.notify(_alerts(with_content=True), {"generated": "test"})

    def test_http_failure_raises(self):
        with self._env():
            with mock.patch.object(
                notify_lark.requests,
                "post",
                return_value=_Response(status_code=500, payload={"code": 1}),
            ):
                with self.assertRaises(notify_lark.LarkDeliveryError):
                    notify_lark.notify(_alerts(with_content=True), {"generated": "test"})

    def test_lark_business_error_raises(self):
        with self._env():
            with mock.patch.object(
                notify_lark.requests,
                "post",
                return_value=_Response(payload={"code": 19021, "msg": "bad sign"}),
            ):
                with self.assertRaises(notify_lark.LarkDeliveryError):
                    notify_lark.notify(_alerts(with_content=True), {"generated": "test"})

    def test_null_false_and_missing_codes_raise(self):
        for payload in ({"code": None}, {"code": False}, {}):
            with self.subTest(payload=payload):
                with self._env():
                    with mock.patch.object(
                        notify_lark.requests,
                        "post",
                        return_value=_Response(payload=payload),
                    ):
                        with self.assertRaises(notify_lark.LarkDeliveryError):
                            notify_lark.notify(
                                _alerts(with_content=True), {"generated": "test"}
                            )

    def test_legacy_status_code_success_and_failure(self):
        with self._env():
            with mock.patch.object(
                notify_lark.requests,
                "post",
                return_value=_Response(payload={"StatusCode": 0}),
            ):
                sent, _ = notify_lark.notify(
                    _alerts(with_content=True), {"generated": "test"}
                )
        self.assertTrue(sent)

        with self._env():
            with mock.patch.object(
                notify_lark.requests,
                "post",
                return_value=_Response(payload={"StatusCode": 1}),
            ):
                with self.assertRaises(notify_lark.LarkDeliveryError):
                    notify_lark.notify(
                        _alerts(with_content=True), {"generated": "test"}
                    )

    def test_invalid_json_raises(self):
        with self._env():
            with mock.patch.object(
                notify_lark.requests,
                "post",
                return_value=_InvalidJsonResponse(),
            ):
                with self.assertRaises(notify_lark.LarkDeliveryError):
                    notify_lark.notify(_alerts(with_content=True), {"generated": "test"})

    def test_notify_empty_forces_a_health_message(self):
        with self._env(LARK_NOTIFY_EMPTY="1"):
            with mock.patch.object(
                notify_lark.requests,
                "post",
                return_value=_Response(payload={"code": 0}),
            ) as post:
                sent, _ = notify_lark.notify(_alerts(), {"generated": "test"})
        self.assertTrue(sent)
        post.assert_called_once()

    def test_success_returns_sent(self):
        with self._env():
            with mock.patch.object(
                notify_lark.requests,
                "post",
                return_value=_Response(payload={"code": 0}),
            ):
                sent, info = notify_lark.notify(
                    _alerts(with_content=True), {"generated": "test"}
                )
        self.assertTrue(sent)
        self.assertIn("已推送", info)

    def test_single_source_round_is_a_non_executable_verification_reminder(self):
        alerts = _alerts()
        alerts["forecasts"] = [{"ticker": "HD"}]
        alerts["rounds"] = [{
            "ticker": "HD", "etype": "dividend", "date": "2026-09-03",
            "days": 2, "forecast": True, "confirmed": False,
            "products": ["现货"], "srcs": ["Alpaca"],
            "ops": "🔎 单源待核实：请核对公司官方公告或第二个独立源；未确认前勿执行。",
        }]
        with mock.patch.object(notify_lark, "_load_mentions", return_value=["ou_owner"]):
            text = json.dumps(
                notify_lark._build_card(alerts, {"generated": "test"}),
                ensure_ascii=False,
            )

        self.assertIn("核验提醒 **1**", text)
        self.assertIn("单源待核实·勿执行", text)
        self.assertIn("单一数据源：Alpaca", text)
        self.assertNotIn("有正式临近催办事项", text)

    def test_formal_round_absorbs_same_event_promotion_update(self):
        alerts = _alerts()
        promoted = {
            "ticker": "HD", "etype": "dividend", "date": "2026-09-03",
            "days": 2, "forecast": False, "confirmed": True,
            "promoted_from_forecast": True, "products": ["现货"],
            "srcs": ["Alpaca", "CompanyIR"], "ops": "正式执行催办",
        }
        alerts["rounds"] = [promoted]
        alerts["forecast_updates"] = [{**promoted, "kind": "declared"}]

        text = json.dumps(
            notify_lark._build_card(alerts, {"generated": "test"}),
            ensure_ascii=False,
        )

        self.assertEqual(1, text.count("**HD**"))
        self.assertIn("单源已转正式", text)
        self.assertNotIn("预测状态更新(自动追踪)", text)

    @staticmethod
    def _event(ticker, **extra):
        return {
            "ticker": ticker,
            "etype": "dividend",
            "date": "2026-09-10",
            "days": 7,
            "products": ["合约"],
            "risk": [],
            **extra,
        }

    @staticmethod
    def _new_group(ticker):
        group = R.EventGroup(
            ticker=ticker,
            etype="dividend",
            anchor_date="2026-09-10",
            by_source={"CompanyIR": {"amount": 1.0}},
            status="confirmed",
        )
        group.value_display = "$1"
        group.value_verified = True
        group.risk = []
        return group

    @staticmethod
    def _new_filing(event_id, *, relevant=True, note="merger agreement"):
        group = R.EventGroup(
            ticker="IBM",
            etype="filing",
            anchor_date="2026-09-10",
            by_source={"SEC": {"url": f"https://sec.example/{event_id}", "note": note}},
            status="confirmed",
            note=note,
        )
        group.event_id = event_id
        group.filing_relevant = relevant
        group.value_display = ""
        group.value_verified = True
        group.risk = []
        group.contract_action = {}
        return group

    def test_card_globally_dedupes_event_sections_by_priority(self):
        alerts = _alerts()
        round_event = self._event("AAPL", ops="execute")
        forecast_update = self._event("HD", kind="declared", official=True)
        contract_update = self._event("IBM", current_status="not_required")
        announced = self._event("QCOM", decl="2026-09-01")
        alerts["rounds"] = [round_event, dict(round_event)]
        alerts["forecast_updates"] = [
            {**round_event, "kind": "declared"},
            forecast_update,
            dict(forecast_update),
        ]
        alerts["contract_updates"] = [
            {**round_event, "current_status": "required"},
            {**forecast_update, "current_status": "required"},
            contract_update,
            dict(contract_update),
        ]
        alerts["announced"] = [round_event, forecast_update, contract_update,
                               announced, dict(announced)]
        alerts["new"] = [
            self._new_group("AAPL"),
            self._new_group("HD"),
            self._new_group("IBM"),
            self._new_group("QCOM"),
            self._new_group("GME"),
            self._new_group("GME"),
        ]

        with mock.patch.object(notify_lark, "_load_mentions", return_value=[]):
            card = notify_lark._build_card(alerts, {"generated": "test"})
        text = json.dumps(card, ensure_ascii=False)

        for ticker in ("AAPL", "HD", "IBM", "QCOM", "GME"):
            self.assertEqual(1, text.count(f"**{ticker}**"), ticker)
        self.assertIn("新公告 **1**", text)
        self.assertIn("执行催办 **1**", text)
        self.assertIn("预测状态 **1**", text)
        self.assertIn("合约结论 **1**", text)
        self.assertIn("新发现 **1**", text)

        visible = notify_lark._visible_alert_items(alerts)
        self.assertEqual(
            {"rounds": 1, "forecast_updates": 1, "contract_updates": 1,
             "announced": 1, "new": 1},
            {key: len(items) for key, items in visible.items()},
        )

    def test_notify_reports_visible_deduped_count(self):
        alerts = _alerts()
        event = self._event("IBM", ops="execute")
        alerts["rounds"] = [event]
        alerts["forecast_updates"] = [{**event, "kind": "declared"}]
        alerts["contract_updates"] = [{**event, "current_status": "required"}]
        alerts["announced"] = [{**event, "decl": "2026-09-01"}]
        alerts["new"] = [self._new_group("IBM")]
        with self._env(), \
                mock.patch.object(notify_lark, "_load_mentions", return_value=[]), \
                mock.patch.object(
                    notify_lark.requests,
                    "post",
                    return_value=_Response(payload={"code": 0}),
                ):
            sent, info = notify_lark.notify(alerts, {"generated": "test"})
        self.assertTrue(sent)
        self.assertIn("已推送 1 条", info)

    def test_filing_event_id_preserves_distinct_same_day_documents(self):
        alerts = _alerts()
        first_id = "IBM|filing|2026-09-10|aaa111"
        second_id = "IBM|filing|2026-09-10|bbb222"
        alerts["rounds"] = [self._event(
            "IBM", etype="filing", event_id=first_id, ops="execute",
        )]
        # 同一文件在低优先级区块被吸收；同日第二份 SEC 文件必须独立保留。
        alerts["forecast_updates"] = [self._event(
            "IBM", etype="filing", event_id=first_id, kind="promoted",
        )]
        alerts["new"] = [
            self._new_filing(first_id, note="first merger filing"),
            self._new_filing(second_id, note="second merger filing"),
        ]

        visible = notify_lark._visible_alert_items(alerts)
        self.assertEqual(1, len(visible["rounds"]))
        self.assertEqual(0, len(visible["forecast_updates"]))
        self.assertEqual(1, len(visible["new"]))
        self.assertEqual(second_id, visible["new"][0].event_id)

        with mock.patch.object(notify_lark, "_load_mentions", return_value=[]):
            text = json.dumps(
                notify_lark._build_card(alerts, {"generated": "test"}),
                ensure_ascii=False,
            )
        self.assertEqual(2, text.count("**IBM**"))
        self.assertIn("执行催办 **1**", text)
        self.assertIn("新发现 **1**", text)

    def test_routine_filing_is_filtered_again_at_delivery_boundary(self):
        alerts = _alerts()
        event_id = "IBM|filing|2026-09-10|routine"
        routine_dict = self._event(
            "IBM", etype="filing", event_id=event_id,
            filing_relevant=False, ops="must never render",
        )
        routine_group = self._new_filing(
            event_id, relevant=False, note="routine 10-Q filing",
        )
        routine_group.conflicts = ["must never render"]
        routine_group.gaps = ["must never render"]
        alerts["rounds"] = [routine_dict]
        alerts["forecast_updates"] = [{**routine_dict, "kind": "updated"}]
        alerts["contract_updates"] = [{**routine_dict, "current_status": "not_required"}]
        alerts["announced"] = [{**routine_dict, "decl": "2026-09-01"}]
        alerts["new"] = [routine_group]
        alerts["conflicts"] = [routine_group]
        alerts["gaps"] = [routine_group]
        alerts["forecasts"] = [routine_dict]
        alerts["review"] = {
            "open": 2, "conflicts": 1, "gaps": 1,
            "overdue": 2, "max_age": 99, "escalate_days": 3,
        }

        visible = notify_lark._visible_alert_items(alerts)
        self.assertTrue(all(not items for items in visible.values()))
        with mock.patch.object(notify_lark, "_load_mentions", return_value=["ou_owner"]):
            card_text = json.dumps(
                notify_lark._build_card(alerts, {"generated": "test"}),
                ensure_ascii=False,
            )
        self.assertNotIn("待人工确认 2 条", card_text)
        self.assertNotIn("<at id=ou_owner></at>", card_text)
        with self._env(), mock.patch.object(notify_lark.requests, "post") as post:
            sent, info = notify_lark.notify(alerts, {"generated": "test"})
        self.assertFalse(sent)
        self.assertIn("无预警内容", info)
        post.assert_not_called()

    def test_unknown_structural_filing_is_not_filtered(self):
        alerts = _alerts()
        unknown = self._new_filing(
            "IBM|filing|2026-09-10|unknown", relevant=None,
            note="unknown structural action",
        )
        alerts["new"] = [unknown]
        visible = notify_lark._visible_alert_items(alerts)
        self.assertEqual([unknown], visible["new"])

    def test_structural_round_does_not_call_filing_date_an_effective_date(self):
        dates = notify_lark._dates({
            "etype": "filing", "date": "2026-09-10",
            "decl": None, "record": None, "pay": None,
        })
        self.assertEqual("事件日 2026-09-10", dates)
        self.assertNotIn("生效", dates)

    def test_contract_message_fallback_keeps_no_action_and_review_copy(self):
        alerts = _alerts()
        alerts["announced"] = [
            self._event(
                "IBM", decl="2026-09-01", risk=[],
                contract_action={
                    "status": "not_required",
                    "message": "合约：本次无需操作｜现金分红影响未超过 3%",
                },
            ),
            self._event(
                "ARM", date="2026-09-11", decl="2026-09-02", risk=[],
                contract_action={
                    "status": "review",
                    "message": "合约：待核实｜缺少可靠参考价，暂不能判定是否操作",
                },
            ),
        ]
        text = json.dumps(
            notify_lark._build_card(alerts, {"generated": "test"}),
            ensure_ascii=False,
        )
        self.assertIn("合约：本次无需操作", text)
        self.assertIn("合约：待核实", text)

    def test_no_action_event_cannot_be_mislabeled_as_execution_round(self):
        alerts = _alerts()
        event_id = "IBM|dividend|2026-09-10"
        no_action = self._event(
            "IBM", event_id=event_id, follow_up_mode="none",
            contract_action={
                "status": "not_required",
                "message": "合约：本次无需操作｜现金分红影响未超过 3%",
            },
        )
        alerts["rounds"] = [{**no_action, "ops": "must never execute"}]
        alerts["announced"] = [{**no_action, "decl": "2026-09-01"}]

        with mock.patch.object(notify_lark, "_load_mentions", return_value=["ou_owner"]):
            card = notify_lark._build_card(alerts, {"generated": "test"})
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn("执行催办 **0**", text)
        self.assertIn("新公告 **1**", text)
        self.assertIn("合约：本次无需操作", text)
        self.assertNotIn("must never execute", text)
        self.assertNotIn("<at id=ou_owner></at>", text)

        website_visible = report._visible_alert_items(alerts)
        self.assertEqual([], website_visible["rounds"])
        self.assertEqual(1, len(website_visible["announced"]))

    def test_announced_only_card_is_blue_and_uses_visible_count(self):
        alerts = _alerts()
        alerts["announced"] = [self._event("QCOM", decl="2026-09-01")]
        card = notify_lark._build_card(alerts, {"generated": "test"})
        text = json.dumps(card, ensure_ascii=False)
        self.assertEqual("blue", card["card"]["header"]["template"])
        self.assertIn("新公告 **1**", text)

    def test_required_contract_update_mentions_owner_without_round(self):
        alerts = _alerts()
        alerts["contract_updates"] = [
            self._event(
                "IBM",
                current_status="required",
                risk=["合约：需操作｜价格影响严格超过 3%"],
            )
        ]
        with mock.patch.object(notify_lark, "_load_mentions", return_value=["ou_owner"]):
            card = notify_lark._build_card(alerts, {"generated": "test"})
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn("<at id=ou_owner></at>", text)
        self.assertIn("有合约结论更新为需操作", text)
        self.assertIn("现已达到 >3% 合约操作门槛", text)

    def test_required_update_still_mentions_when_higher_priority_section_absorbs_it(self):
        alerts = _alerts()
        event_id = "IBM|dividend|2026-09-10"
        alerts["forecast_updates"] = [self._event(
            "IBM", event_id=event_id, kind="updated",
            previous_date="2026-09-09", previous_amount=0.1,
            risk=["合约：需操作｜现金分红影响严格超过 3%"],
        )]
        alerts["contract_updates"] = [self._event(
            "IBM", event_id=event_id, current_status="required",
            risk=["合约：需操作｜现金分红影响严格超过 3%"],
        )]
        with mock.patch.object(notify_lark, "_load_mentions", return_value=["ou_owner"]):
            card = notify_lark._build_card(alerts, {"generated": "test"})
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn("<at id=ou_owner></at>", text)
        self.assertIn("有合约结论更新为需操作", text)
        self.assertIn("预测状态 **1**", text)
        self.assertIn("合约结论 **0**", text)
        self.assertNotIn("合约操作结论更新", text)


class ReminderCadenceTests(unittest.TestCase):
    def test_single_source_uses_30_14_cadence_without_becoming_confirmed(self):
        fired = {}
        event = {
            "ticker": "HD", "etype": "dividend", "date": "2026-09-20",
            "days": 20, "forecast": True, "confirmed": False,
            "risk": ["不应进入单源核验提醒"],
        }

        headsup = run.schedule_event_reminder(event, "HD|dividend|2026-09-20", fired, "2026-08-31")
        duplicate = run.schedule_event_reminder(event, "HD|dividend|2026-09-20", fired, "2026-08-31")
        daily = run.schedule_event_reminder(
            {**event, "days": 14}, "HD|dividend|2026-09-20", fired, "2026-09-06"
        )

        self.assertEqual("headsup", headsup["cadence"])
        self.assertIsNone(duplicate)
        self.assertEqual("daily", daily["cadence"])
        self.assertTrue(headsup["forecast"])
        self.assertFalse(headsup["confirmed"])
        self.assertIn("未确认前勿执行", headsup["ops"])
        self.assertEqual([], headsup["risk"])
        self.assertIn("HD|dividend|2026-09-20#verification", fired)

        formal = run.schedule_event_reminder(
            {**event, "forecast": False, "confirmed": True, "days": 14},
            "HD|dividend|2026-09-20", fired, "2026-09-06",
        )
        self.assertIsNotNone(formal)
        self.assertEqual("daily", formal["cadence"])
        self.assertIn("HD|dividend|2026-09-20", fired)


class StateOrderingTests(unittest.TestCase):
    def test_delivery_failure_does_not_save_state(self):
        with mock.patch.object(
            run.notify_lark,
            "notify",
            side_effect=notify_lark.LarkDeliveryError("failed"),
        ):
            with mock.patch.object(run, "save_state") as save:
                with self.assertRaises(notify_lark.LarkDeliveryError):
                    run.deliver_then_save(_alerts(), {"generated": "test"}, {"seen": {}})
        save.assert_not_called()

    def test_legal_skip_saves_state(self):
        state = {"seen": {}}
        with mock.patch.object(
            run.notify_lark,
            "notify",
            return_value=(False, "legal skip"),
        ):
            with mock.patch.object(run, "save_state") as save:
                run.deliver_then_save(_alerts(), {"generated": "test"}, state)
        save.assert_called_once_with(state)

    def test_success_saves_state(self):
        state = {"seen": {}}
        with mock.patch.object(
            run.notify_lark,
            "notify",
            return_value=(True, "sent"),
        ):
            with mock.patch.object(run, "save_state") as save:
                run.deliver_then_save(_alerts(), {"generated": "test"}, state)
        save.assert_called_once_with(state)


if __name__ == "__main__":
    unittest.main()
