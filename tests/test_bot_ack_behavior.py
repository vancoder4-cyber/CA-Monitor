# -*- coding: utf-8 -*-
import importlib.util
import base64
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("ca_monitor_bot_ack", ROOT / "bot" / "ack.py")
ack = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ack)


class _Response:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class AckReferenceTests(unittest.TestCase):
    def test_only_dividend_may_use_dividend_ir(self):
        refs = {"IBM": "https://example.test/ibm-dividend-ir"}
        filing = "https://www.sec.gov/Archives/example-filing"

        self.assertEqual(
            refs["IBM"],
            ack.authoritative_source("IBM", "dividend", refs_ir=refs, src_url=filing),
        )
        self.assertEqual(
            filing,
            ack.authoritative_source("IBM", "split", refs_ir=refs, src_url=filing),
        )
        self.assertEqual(
            filing,
            ack.authoritative_source("IBM", "filing", refs_ir=refs, src_url=filing),
        )

    def test_split_and_filing_verify_links_ignore_dividend_ir(self):
        refs = {"IBM": "https://example.test/ibm-dividend-ir"}
        filing = "https://www.sec.gov/Archives/example-filing"

        self.assertEqual(
            (filing, "SEC原文", 2),
            ack.verify_link("IBM", "split", filing, refs_ir=refs),
        )
        self.assertEqual(
            (filing, "SEC原文", 2),
            ack.verify_link("IBM", "filing", filing, refs_ir=refs),
        )

        url, label, tier = ack.verify_link("IBM", "split", refs_ir=refs)
        self.assertIn("sec.gov/cgi-bin/browse-edgar", url)
        self.assertNotEqual(refs["IBM"], url)
        self.assertEqual(("SEC·公司备案", 2), (label, tier))


class AckValueGateTests(unittest.TestCase):
    def test_valid_positive_amount_and_split_formats_are_accepted(self):
        for value, etype in (("0.26", "dividend"), ("1:10", "split"),
                             ("1：10", "split"), ("4", "split"), ("0.1", "split")):
            with self.subTest(value=value, etype=etype):
                self.assertTrue(ack.is_valid_confirmation_value(value, etype))

    def test_empty_nonpositive_nonfinite_and_malformed_values_are_rejected(self):
        for value, etype in (
            (None, "dividend"), ("", "dividend"), (0, "dividend"),
            ("-0.26", "dividend"), ("NaN", "dividend"), ("Infinity", "dividend"),
            ("1e2", "dividend"), ("1_000", "dividend"),
            ("1e10000", "dividend"), ("1e-10000", "dividend"),
            (None, "split"), ("1:0", "split"), ("-1:10", "split"),
            ("+1:+10", "split"),
            ("1:", "split"), ("abc", "split"), ("1:10000000", "split"),
            ("0.0000001", "split"), ("1:10", "dividend"),
        ):
            with self.subTest(value=value, etype=etype):
                self.assertFalse(ack.is_valid_confirmation_value(value, etype))

    def test_start_scoped_parser_does_not_take_number_from_operator_note(self):
        self.assertEqual(
            (None, None),
            ack.parse_confirm_value(" 已比对公司 8-K", at_start=True),
        )
        self.assertEqual(
            ("0.26", " $0.26"),
            ack.parse_confirm_value(" $0.26 已比对公司 8-K", at_start=True),
        )
        self.assertEqual(("1", "1"), ack.parse_confirm_value("1, note", at_start=True))
        self.assertEqual(("1:10", "1-10"), ack.parse_confirm_value("1-10", at_start=True))
        self.assertEqual(("1:10", "1 for 10"), ack.parse_confirm_value("1 for 10", at_start=True))
        for text in ("1e2", "1,000", "8-K checked"):
            with self.subTest(text=text):
                self.assertEqual((None, None), ack.parse_confirm_value(text, at_start=True))

    @mock.patch.object(ack, "GH_TOKEN", "test-token")
    @mock.patch.object(ack, "_get_file")
    @mock.patch.object(ack, "_put_file")
    def test_writeback_rejects_invalid_value_before_any_github_io(self, put_file, get_file):
        for value, etype in ((None, "dividend"), ("", "dividend"),
                             ("0", "dividend"), ("1:0", "split"),
                             ("4", "split"), ("1-10", "split"),
                             ("1 for 10", "split")):
            with self.subTest(value=value, etype=etype):
                ok, message = ack.add_ack("IBM", value, etype, "2026-09-10")
                self.assertFalse(ok)
                self.assertIn("本次未写入任何状态", message)
        get_file.assert_not_called()
        put_file.assert_not_called()


class AckPartialWriteTests(unittest.TestCase):
    @mock.patch.object(ack, "GH_TOKEN", "test-token")
    @mock.patch.object(ack, "_get_file")
    @mock.patch.object(ack, "_put_file")
    def test_ack_core_write_failure_is_not_reported_as_success(self, put_file, get_file):
        get_file.side_effect = [([], "ack-sha"), ([], "log-sha")]
        put_file.side_effect = [_Response(200), _Response(409, "sha conflict")]

        ok, message = ack.add_ack(
            "IBM", "1:10", "split", "2026-09-10", refs_ir={"IBM": "https://example.test/dividend"},
        )

        self.assertFalse(ok)
        self.assertIn("仅留痕、未生效", message)
        self.assertIn("报警不会解除", message)
        self.assertIn("HTTP 409", message)
        self.assertEqual(ack.LOG_PATH, put_file.call_args_list[0].args[0])
        self.assertEqual(ack.ACK_PATH, put_file.call_args_list[1].args[0])

    @mock.patch.object(ack, "GH_TOKEN", "test-token")
    @mock.patch.object(ack, "_get_file")
    @mock.patch.object(ack, "_put_file")
    def test_forecast_core_write_failure_is_not_reported_as_success(self, put_file, get_file):
        get_file.side_effect = [([], "forecast-sha"), ([], "log-sha")]
        put_file.side_effect = [_Response(201), _Response(500, "server error")]

        ok, message = ack.add_forecast(
            "IBM", "dividend", "2026-09-10", refs_ir={"IBM": "https://example.test/dividend"},
        )

        self.assertFalse(ok)
        self.assertIn("仅留痕、未生效", message)
        self.assertIn("HTTP 500", message)
        self.assertEqual(ack.LOG_PATH, put_file.call_args_list[0].args[0])
        self.assertEqual(ack.FORECAST_PATH, put_file.call_args_list[1].args[0])


class RequestPrivacyTests(unittest.TestCase):
    @mock.patch.object(ack, "GH_TOKEN", "test-token")
    @mock.patch.object(ack.requests, "get")
    @mock.patch.object(ack.requests, "put")
    def test_public_request_omits_lark_identity_and_raw_text_from_commit(self, put, get):
        get.return_value = _Response(404)
        put.return_value = _Response(201)
        requester = "ou_should_never_be_public"
        body_text = "请增加财报日提醒"

        ok, message = ack.add_request(body_text, by=requester)

        self.assertTrue(ok, message)
        payload = put.call_args.kwargs["json"]
        public_markdown = base64.b64decode(payload["content"]).decode("utf-8")
        self.assertIn(body_text, public_markdown)
        self.assertIn("编号 ", public_markdown)
        self.assertNotIn(requester, public_markdown)
        self.assertNotIn("提报人", public_markdown)
        self.assertNotIn(body_text, payload["message"])
        self.assertRegex(payload["message"], r"^request: add bot submission [0-9a-f]{10}$")


if __name__ == "__main__":
    unittest.main()
