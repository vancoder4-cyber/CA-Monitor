import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import notify_lark
import run


def _alerts(with_content=False):
    return {
        "new": [],
        "rounds": [],
        "conflicts": [],
        "gaps": [],
        "pending": [{}] if with_content else [],
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
