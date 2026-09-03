# -*- coding: utf-8 -*-
"""End-to-end guards for the SEC filing review closure lifecycle."""
import datetime as dt
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest import mock

import config as C
import notify_lark
import reconcile
import report
import run
from bot import cards


ROOT = Path(__file__).resolve().parents[1]
_ACK_SPEC = importlib.util.spec_from_file_location(
    "ca_monitor_filing_ack", ROOT / "bot" / "ack.py"
)
ack = importlib.util.module_from_spec(_ACK_SPEC)
_ACK_SPEC.loader.exec_module(ack)

FILING_DATE = "2026-09-03"
SEC_URL_A = "https://www.sec.gov/Archives/edgar/data/1/0001/report-a.htm"
SEC_URL_B = "https://www.sec.gov/Archives/edgar/data/1/0001/report-b.htm"


class _Response:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def _filing(url=SEC_URL_A, *, note="6-K · dividend distribution terms", relevant=None):
    return reconcile.EventGroup(
        ticker="IBM",
        etype="filing",
        anchor_date=FILING_DATE,
        by_source={
            "SEC": {
                "ex_date": FILING_DATE,
                "url": url,
                "form": "6-K",
                "note": note,
                "relevant": relevant,
            }
        },
        sources_ok=["SEC"],
        status="single",
        note=note,
    )


def _dividend(*, declaration_date=FILING_DATE):
    fields = {
        "ex_date": "2026-09-20",
        "declaration_date": declaration_date,
        "record_date": "2026-09-21",
        "pay_date": "2026-10-01",
        "amount": 0.20,
        "amount_currency": "USD",
        "amount_unit": "listed_security",
        "subtype": "cash_dividend",
    }
    return reconcile.EventGroup(
        ticker="IBM",
        etype="dividend",
        anchor_date="2026-09-20",
        by_source={"Alpaca": dict(fields), "FINX": dict(fields)},
        sources_ok=["Alpaca", "FINX"],
        status="confirmed",
    )


class StableFilingIdTests(unittest.TestCase):
    def test_same_day_sec_documents_have_distinct_ids_but_source_enrichment_is_stable(self):
        first = _filing(SEC_URL_A, note="first wording")
        same_document = _filing(SEC_URL_A, note="later translated wording")
        same_document.by_source["Alpaca"] = {
            "ex_date": FILING_DATE,
            "note": "vendor enrichment",
            "relevant": None,
        }
        second = _filing(SEC_URL_B, note="second filing")

        first_id = run.sig(first)
        self.assertRegex(
            first_id,
            r"^IBM\|filing\|2026-09-03\|[0-9a-f]{12}$",
        )
        self.assertEqual(first_id, run.sig(same_document))
        self.assertNotEqual(first_id, run.sig(second))

    def test_resolution_applies_to_only_the_exact_filing_id(self):
        first, second = _filing(SEC_URL_A), _filing(SEC_URL_B)
        first.event_id, second.event_id = run.sig(first), run.sig(second)
        resolution = [{
            "event_id": first.event_id,
            "ticker": "IBM",
            "date": FILING_DATE,
            "status": "routine",
        }]

        matched = run.apply_filing_review_resolutions(
            {"IBM": [first, second]}, resolution, allowed_tickers={"IBM"}
        )

        self.assertEqual({first.event_id}, matched)
        self.assertEqual("routine", first.filing_resolution_status)
        self.assertFalse(hasattr(second, "filing_resolution_status"))
        run.attach_product_action(first, None, dt.date(2026, 9, 4))
        run.attach_product_action(second, None, dt.date(2026, 9, 4))
        self.assertIs(first.filing_relevant, False)
        self.assertIsNone(second.filing_relevant)


class FilingResolutionWritebackTests(unittest.TestCase):
    @mock.patch.object(ack, "GH_TOKEN", "test-token")
    @mock.patch.object(ack, "_get_file")
    @mock.patch.object(ack, "_put_file")
    def test_writeback_replaces_only_exact_id_and_keeps_identity_and_note_private(
            self, put_file, get_file):
        first_id = run.sig(_filing(SEC_URL_A))
        second_id = run.sig(_filing(SEC_URL_B))
        prior = [
            {"event_id": first_id, "ticker": "IBM", "date": FILING_DATE,
             "status": "confirmed"},
            {"event_id": second_id, "ticker": "IBM", "date": FILING_DATE,
             "status": "confirmed"},
        ]
        get_file.side_effect = [(prior, "resolution-sha"), ([], "log-sha")]
        put_file.side_effect = [_Response(200), _Response(200)]

        ok, message = ack.resolve_filing_review(
            first_id,
            "routine",
            ticker="IBM",
            date=FILING_DATE,
            by="ou_private",
            by_name="Private Operator",
            note="terms checked",
            src_url=SEC_URL_A,
        )

        self.assertTrue(ok, message)
        self.assertEqual(ack.LOG_PATH, put_file.call_args_list[0].args[0])
        log_rows = put_file.call_args_list[0].args[1]
        self.assertEqual("resolve_filing_review", log_rows[-1]["action"])
        self.assertNotIn("ou_private", json.dumps(log_rows, ensure_ascii=False))
        self.assertNotIn("Private Operator", json.dumps(log_rows, ensure_ascii=False))
        self.assertNotIn("terms checked", json.dumps(log_rows, ensure_ascii=False))
        self.assertEqual("system", log_rows[-1]["by"])
        self.assertEqual("", log_rows[-1]["by_name"])
        self.assertEqual("", log_rows[-1]["note"])

        self.assertEqual(ack.FILING_RESOLUTION_PATH, put_file.call_args_list[1].args[0])
        active_rows = put_file.call_args_list[1].args[1]
        by_id = {row["event_id"]: row for row in active_rows}
        self.assertEqual({first_id, second_id}, set(by_id))
        self.assertEqual("routine", by_id[first_id]["status"])
        self.assertEqual("confirmed", by_id[second_id]["status"])
        self.assertNotIn("by", by_id[first_id])
        self.assertNotIn("by_name", by_id[first_id])
        self.assertNotIn("note", by_id[first_id])
        self.assertNotIn("ou_private", json.dumps(active_rows, ensure_ascii=False))
        self.assertNotIn("Private Operator", json.dumps(active_rows, ensure_ascii=False))
        self.assertNotIn("terms checked", json.dumps(active_rows, ensure_ascii=False))

    @mock.patch.object(ack, "GH_TOKEN", "test-token")
    @mock.patch.object(ack, "_get_file")
    @mock.patch.object(ack, "_put_file")
    def test_broad_or_malformed_id_is_rejected_without_writes(self, put_file, get_file):
        ok, message = ack.resolve_filing_review(
            "IBM|filing|2026-09-03", "routine"
        )
        self.assertFalse(ok)
        self.assertIn("event_id 无效", message)
        get_file.assert_not_called()
        put_file.assert_not_called()

    @mock.patch.object(ack, "GH_TOKEN", "test-token")
    @mock.patch.object(ack, "_get_file")
    @mock.patch.object(ack, "_put_file")
    def test_resolution_state_failure_is_never_reported_as_success(
            self, put_file, get_file):
        event_id = run.sig(_filing())
        get_file.side_effect = [([], "resolution-sha"), ([], "log-sha")]
        put_file.side_effect = [_Response(201), _Response(409, "sha conflict")]

        ok, message = ack.resolve_filing_review(event_id, "routine")

        self.assertFalse(ok)
        self.assertIn("仅留痕、未生效", message)
        self.assertIn("核验提醒不会关闭", message)


class AutoLinkAndExpiryTests(unittest.TestCase):
    def test_exact_dividend_declaration_date_links_6k_without_a_second_ca_item(self):
        dividend = _dividend()
        filing = _filing()
        dividend.event_id, filing.event_id = run.sig(dividend), run.sig(filing)

        linked = run.link_dividend_6k_evidence({"IBM": [dividend, filing]})
        run.attach_product_action(dividend, None, dt.date(2026, 9, 4))
        run.attach_product_action(filing, None, dt.date(2026, 9, 4))
        run.attach_event_references(dividend, {"ir_dividend": {}}, {})

        self.assertEqual({filing.event_id: dividend.event_id}, linked)
        self.assertEqual("linked", filing.filing_resolution_status)
        self.assertEqual(dividend.event_id, filing.linked_event_id)
        self.assertIs(filing.filing_relevant, False)
        self.assertEqual(SEC_URL_A, dividend.linked_sec_url)
        self.assertEqual(
            1,
            sum(1 for ref in dividend.references if ref.get("url") == SEC_URL_A),
        )

        event = {
            "ticker": "IBM", "etype": "filing", "date": FILING_DATE,
            "event_id": filing.event_id, "filing_relevant": False,
        }
        self.assertEqual([], cards._non_routine([event]))
        self.assertEqual([], notify_lark._non_routine([event]))
        self.assertEqual([], report._non_routine([event]))

    def test_mismatched_date_mixed_terms_and_ambiguous_dividends_do_not_auto_link(self):
        cases = [
            ([_dividend(declaration_date="2026-09-02"), _filing()], "date mismatch"),
            ([_dividend(), _filing(note="6-K · dividend and merger terms")], "mixed action"),
            ([_dividend(), _dividend(), _filing()], "ambiguous dividends"),
        ]
        for groups, label in cases:
            with self.subTest(label=label):
                for group in groups:
                    group.event_id = run.sig(group)
                self.assertEqual({}, run.link_dividend_6k_evidence({"IBM": groups}))
                filing = next(group for group in groups if group.etype == "filing")
                self.assertFalse(hasattr(filing, "filing_resolution_status"))

    def test_review_expires_once_after_day_30_and_never_reopens(self):
        event_id = run.sig(_filing())
        state = {
            event_id: {
                "status": "review",
                "last_seen": FILING_DATE,
                "event": {
                    "ticker": "IBM", "etype": "filing", "date": FILING_DATE,
                    "event_id": event_id, "filing_relevant": None,
                    "verification_kind": "filing_terms",
                    "follow_up_mode": "verification", "risk": ["verify terms"],
                },
            }
        }

        day_30 = run.collect_overdue_filing_reviews(state, "2026-10-03")
        self.assertEqual("review_pending", day_30[0]["kind"])
        self.assertEqual("review", state[event_id]["status"])

        day_31 = run.collect_overdue_filing_reviews(state, "2026-10-04")
        self.assertEqual(1, len(day_31))
        self.assertEqual("expired", day_31[0]["kind"])
        self.assertEqual("none", day_31[0]["follow_up_mode"])
        self.assertEqual("filing_terms", day_31[0]["verification_kind"])
        self.assertIsNone(day_31[0]["filing_relevant"])
        self.assertEqual([], day_31[0]["risk"])
        self.assertEqual("expired", state[event_id]["status"])

        alerts = {
            "filing_updates": day_31,
            "rounds": [], "forecast_updates": [], "contract_updates": [],
            "announced": [], "new": [], "conflicts": [], "gaps": [],
            "forecasts": [], "pending": [], "review": {},
        }
        with mock.patch.object(notify_lark, "_load_mentions", return_value=["ou_owner"]):
            lark_text = json.dumps(
                notify_lark._build_card(alerts, {"generated": "test"}),
                ensure_ascii=False,
            )
        website_text = report.build_text_digest(
            alerts, {"generated": "test", "business_date": "2026-10-04"}
        )
        bot_text = json.dumps(cards.alert_card({
            "counts": {}, "filing_updates": day_31, "refs": {},
            "generated": "test", "conflicts": [], "gaps": [],
        }, ""), ensure_ascii=False)
        for surface_text in (lark_text, website_text, bot_text):
            self.assertIn("已停止每日提醒", surface_text)
            self.assertIn("事项仍未核实", surface_text)
            self.assertIn("不得据此判断无需操作或执行", surface_text)
            self.assertNotIn("本次无需操作", surface_text)
        self.assertNotIn("<at id=ou_owner></at>", lark_text)

        self.assertEqual([], run.collect_overdue_filing_reviews(state, "2026-10-05"))
        stale = _filing()
        stale.event_id = event_id
        run.attach_product_action(stale, None, dt.date(2026, 10, 5))
        self.assertIsNone(
            run.track_filing_relevance(stale, event_id, state, "2026-10-05")
        )
        self.assertEqual("expired", state[event_id]["status"])

    def test_resolution_of_missing_expired_filing_emits_once(self):
        event_id = run.sig(_filing())
        state = {
            event_id: {
                "status": "expired",
                "event": {
                    "ticker": "IBM", "etype": "filing", "date": FILING_DATE,
                    "event_id": event_id, "filing_relevant": None,
                    "verification_kind": "filing_terms",
                    "follow_up_mode": "verification", "risk": ["verify terms"],
                },
            }
        }
        resolution = [{"event_id": event_id, "status": "routine"}]

        first = run.apply_missing_filing_resolutions(
            state, resolution, current_event_ids=set(), today="2026-10-05"
        )
        second = run.apply_missing_filing_resolutions(
            state, resolution, current_event_ids=set(), today="2026-10-05"
        )

        self.assertEqual(1, len(first))
        self.assertEqual("routine", first[0]["kind"])
        self.assertEqual("none", first[0]["follow_up_mode"])
        self.assertEqual([], first[0]["risk"])
        self.assertEqual([], second)

    def test_missing_confirmed_filing_leaves_terms_review_for_product_action(self):
        event_id = run.sig(_filing())

        def resolve(*, spot, contract):
            state = {
                event_id: {
                    "status": "review",
                    "event": {
                        "ticker": "IBM", "etype": "filing", "date": FILING_DATE,
                        "event_id": event_id, "filing_relevant": None,
                        "products": (["现货"] if spot else []) + (["合约"] if contract else []),
                        "contract_action": {
                            "status": "review",
                            "message": "合约：待核实｜结构性公司行动需确认条款及价格影响是否超过 3%",
                        },
                        "verification_kind": "filing_terms",
                        "follow_up_mode": "verification",
                        "reminder_state_suffix": "filing-review",
                        "risk": ["公司行动条款仍待核实"],
                    },
                }
            }
            with mock.patch.object(C, "SPOT_TICKERS", {"IBM"} if spot else set()), \
                    mock.patch.object(C, "CONTRACT_TICKERS", {"IBM"} if contract else set()):
                updates = run.apply_missing_filing_resolutions(
                    state,
                    [{"event_id": event_id, "status": "confirmed"}],
                    current_event_ids=set(),
                    today="2026-09-04",
                )
            self.assertEqual(1, len(updates))
            return updates[0]

        spot_and_contract = resolve(spot=True, contract=True)
        self.assertTrue(spot_and_contract["filing_relevant"])
        self.assertEqual("execution", spot_and_contract["follow_up_mode"])
        self.assertEqual("", spot_and_contract["verification_kind"])
        self.assertEqual("", spot_and_contract["reminder_state_suffix"])
        self.assertEqual("review", spot_and_contract["contract_action"]["status"])
        self.assertTrue(any(line.startswith("现货：") for line in spot_and_contract["risk"]))
        self.assertTrue(any(line.startswith("合约：待核实") for line in spot_and_contract["risk"]))

        contract_only = resolve(spot=False, contract=True)
        self.assertTrue(contract_only["filing_relevant"])
        self.assertEqual("verification", contract_only["follow_up_mode"])
        self.assertEqual("contract_threshold", contract_only["verification_kind"])
        self.assertEqual("contract-review", contract_only["reminder_state_suffix"])
        self.assertEqual("review", contract_only["contract_action"]["status"])
        self.assertTrue(all("公司行动条款仍待核实" not in line
                            for line in contract_only["risk"]))


class FilingResolutionBuildIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.paths = {
            "state": self.root / "state.json",
            "site": self.root / "site_data.json",
            "page": self.root / "dashboard.html",
            "digest": self.root / "digest.txt",
        }
        self.current_day = dt.date(2026, 9, 3)
        self.resolutions = []
        self.stack = ExitStack()
        for patcher in (
            mock.patch.object(run, "CACHE", str(self.cache)),
            mock.patch.object(run, "STATE_PATH", str(self.paths["state"])),
            mock.patch.object(run, "FORECAST_WATCH_PATH", str(self.root / "forecast_watch.json")),
            mock.patch.object(run, "OUT_HTML", str(self.paths["page"])),
            mock.patch.object(run, "OUT_DIGEST", str(self.paths["digest"])),
            mock.patch.object(run, "OUT_SITEDATA", str(self.paths["site"])),
            mock.patch.object(run, "business_today", side_effect=lambda: self.current_day),
            mock.patch.object(reconcile, "business_today", side_effect=lambda: self.current_day),
            mock.patch.object(run, "load_refs", return_value={"ir_dividend": {}}),
            mock.patch.object(run, "load_acknowledged", return_value=[]),
            mock.patch.object(run, "load_forecast_watches", return_value=[]),
            mock.patch.object(
                run, "load_filing_review_resolutions",
                side_effect=lambda: list(self.resolutions),
            ),
            mock.patch.object(run.notify_lark, "notify", return_value=(False, "test skip")),
            mock.patch.object(C, "TICKERS", ["IBM"]),
            mock.patch.object(C, "ALL_ASSETS", ["IBM"]),
            mock.patch.object(C, "SPOT_TICKERS", {"IBM"}),
            mock.patch.object(C, "CONTRACT_TICKERS", set()),
        ):
            self.stack.enter_context(patcher)

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def _write_cache(self, groups):
        (self.cache / "IBM.json").write_text(json.dumps({
            "ticker": "IBM",
            "fetched": f"{self.current_day.isoformat()}T12:00:00",
            "health": {"SEC": "ok", "Alpaca": "ok", "FINX": "ok"},
            "groups": [group.to_dict() for group in groups],
        }), encoding="utf-8")

    def _build(self):
        with redirect_stdout(io.StringIO()):
            return run.build()

    def test_bot_resolution_file_closes_current_candidate_once_across_all_outputs(self):
        filing = _filing()
        self._write_cache([filing])
        first = self._build()
        event_id = first["rounds"][0]["event_id"]
        self.assertEqual("review", json.loads(
            self.paths["state"].read_text(encoding="utf-8")
        )["filing_relevance_status"][event_id]["status"])

        self.resolutions.append({
            "event_id": event_id, "ticker": "IBM", "date": FILING_DATE,
            "status": "routine", "source": SEC_URL_A,
        })
        resolved = self._build()
        self.assertEqual(1, len(resolved["filing_updates"]))
        self.assertEqual("routine", resolved["filing_updates"][0]["kind"])
        self.assertEqual([], resolved["pending"])
        self.assertEqual([], resolved["rounds"])

        site = json.loads(self.paths["site"].read_text(encoding="utf-8"))
        self.assertEqual([], site["pending"])
        self.assertEqual([], site["calendar"])
        self.assertEqual("routine", site["filing_updates"][0]["kind"])
        self.assertIn("普通备案，不属于公司行动；本次无需操作",
                      self.paths["digest"].read_text(encoding="utf-8"))
        self.assertIn("普通备案，不属于公司行动；本次无需操作",
                      self.paths["page"].read_text(encoding="utf-8"))

        stable = self._build()
        self.assertEqual([], stable["filing_updates"])
        self.assertEqual([], stable["rounds"])

    def test_auto_link_build_keeps_only_dividend_in_business_surfaces(self):
        self._write_cache([_dividend(), _filing()])
        alerts = self._build()
        site = json.loads(self.paths["site"].read_text(encoding="utf-8"))

        self.assertEqual(["dividend"], [event["etype"] for event in alerts["pending"]])
        self.assertEqual([], alerts["filing_updates"])
        self.assertEqual(["dividend"], [event["etype"] for event in site["calendar"]])
        dividend = site["pending"][0]
        self.assertEqual(SEC_URL_A, dividend["linked_sec_url"])
        self.assertTrue(dividend["linked_filing_event_id"].startswith(
            "IBM|filing|2026-09-03|"
        ))
        self.assertEqual(
            1, sum(1 for ref in dividend["references"] if ref.get("url") == SEC_URL_A)
        )

        lookup_text = json.dumps(cards.lookup_card(site, "IBM", ""), ensure_ascii=False)
        self.assertIn(SEC_URL_A, lookup_text)
        self.assertNotIn("疑似公司行动", lookup_text)
        self.assertNotIn("确认备案", lookup_text)


class CrossSurfaceDedupTests(unittest.TestCase):
    def test_status_update_wins_but_same_day_distinct_filings_both_survive(self):
        first_id = run.sig(_filing(SEC_URL_A))
        second_id = run.sig(_filing(SEC_URL_B))
        update = {
            "ticker": "IBM", "etype": "filing", "date": FILING_DATE,
            "event_id": first_id, "kind": "routine", "current_status": "routine",
            "filing_relevant": False, "follow_up_mode": "none",
        }
        duplicate_round = {**update, "filing_relevant": None,
                           "follow_up_mode": "verification"}
        other_filing = {
            "ticker": "IBM", "etype": "filing", "date": FILING_DATE,
            "event_id": second_id, "filing_relevant": None,
            "follow_up_mode": "verification", "verification_kind": "filing_terms",
        }
        alerts = {
            "filing_updates": [update],
            "rounds": [duplicate_round, other_filing],
            "forecast_updates": [], "contract_updates": [],
            "announced": [], "new": [duplicate_round, other_filing],
        }

        for surface in (notify_lark._visible_alert_items, report._visible_alert_items):
            with self.subTest(surface=surface.__module__):
                visible = surface(alerts)
                self.assertEqual([first_id], [x["event_id"] for x in visible["filing_updates"]])
                self.assertEqual([second_id], [x["event_id"] for x in visible["rounds"]])
                self.assertEqual([], visible["new"])


if __name__ == "__main__":
    unittest.main()
