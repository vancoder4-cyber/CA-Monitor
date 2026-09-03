import datetime as dt
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sources
import config as C
import run
import reconcile as R


class SecForeignFormTests(unittest.TestCase):
    def test_8ka_uses_same_item_gate_as_8k(self):
        note, relevant = sources._sec_filing_note_relevance(
            "8-K/A", "3.03,8.01", "amended.htm", "8-K/A",
        )
        self.assertTrue(relevant)
        self.assertIn("3.03", note)

        _note, relevant = sources._sec_filing_note_relevance(
            "8-K/A", "2.02,8.01", "earnings.htm", "8-K/A",
        )
        self.assertFalse(relevant)

    def test_6k_only_strong_metadata_hint_enters_review(self):
        note, relevant = sources._sec_filing_note_relevance(
            "6-K", "", "tsm-dividendadjustmentx202.htm", "6-K",
        )
        self.assertIsNone(relevant)
        self.assertIn("疑似公司行动", note)
        self.assertIn("分红", note)

        note, relevant = sources._sec_filing_note_relevance(
            "6-K", "", "arm-20260810.htm", "6-K",
        )
        self.assertFalse(relevant)
        self.assertIn("普通备案", note)

        note, relevant = sources._sec_filing_note_relevance(
            "6-K/A", "", "issuer-reverse-split-amendment.htm", "6-K/A",
        )
        self.assertIsNone(relevant)
        self.assertIn("6-K/A", note)
        self.assertIn("合股", note)

    def test_6k_routine_financing_and_results_phrases_stay_out(self):
        for document, description in (
            ("distribution-agreement.htm", "Distribution Agreement"),
            ("financial-results.htm", "Consolidation of Financial Results"),
            ("senior-notes.htm", "Notice of Redemption of Senior Notes"),
            ("report-human-rights-issues.htm", "Report on Human Rights Issues"),
            ("humanrightsissues.htm", "Human Rights Issues"),
        ):
            with self.subTest(description=description):
                note, relevant = sources._sec_filing_note_relevance(
                    "6-K", "", document, description,
                )
                self.assertFalse(relevant)
                self.assertIn("普通备案", note)

    def test_suspected_6k_note_does_not_promote_itself_to_execution(self):
        for action, label in (
            ("issuer-merger.htm", "并购"),
            ("issuer-spin-off.htm", "分拆"),
            ("issuer-delisting.htm", "退市"),
            ("issuer-tender-offer.htm", "要约收购"),
        ):
            with self.subTest(action=action):
                note, relevant = sources._sec_filing_note_relevance(
                    "6-K", "", action, "FORM 6-K",
                )
                self.assertIsNone(relevant)
                self.assertIn(label, note)
                group = SimpleNamespace(
                    ticker="BB", etype="filing", note=note,
                    by_source={"SEC": {"note": note, "relevant": None}},
                    conflicts=[],
                )
                with mock.patch.object(C, "SPOT_TICKERS", {"BB"}):
                    run.attach_product_action(
                        group, reference_price=None,
                        today=dt.date(2026, 9, 3), forecast=False,
                    )
                self.assertIsNone(group.filing_relevant)
                self.assertEqual("verification", group.follow_up_mode)

    def test_fetch_sec_keeps_generic_6k_raw_and_flags_only_candidates(self):
        recent = {
            "form": ["6-K", "6-K", "8-K/A", "8-K/A"],
            "filingDate": ["2026-09-01"] * 4,
            "accessionNumber": [
                "0001-26-000001", "0001-26-000002",
                "0001-26-000003", "0001-26-000004",
            ],
            "primaryDocument": [
                "ordinary-report.htm", "issuer-stock-split.htm",
                "structural-amendment.htm", "earnings-amendment.htm",
            ],
            "primaryDocDescription": ["FORM 6-K", "6-K", "8-K/A", "8-K/A"],
            "items": ["", "", "3.03,8.01", "2.02,8.01"],
            "acceptanceDateTime": ["2026-09-01T12:00:00.000Z"] * 4,
        }
        response = mock.Mock(status_code=200)
        response.json.return_value = {"filings": {"recent": recent}}
        with (
            mock.patch.object(sources, "_load_cik_map", return_value={"TSM": "0001046179"}),
            mock.patch.object(sources.requests, "get", return_value=response),
            mock.patch.object(sources, "business_today", return_value=dt.date(2026, 9, 3)),
        ):
            result = sources.fetch_sec("TSM", 90)

        self.assertEqual("ok", result.status)
        self.assertEqual(4, len(result.events))
        self.assertEqual([False, None, True, False], [
            event.raw.get("relevant") for event in result.events
        ])
        self.assertEqual("FORM 6-K", result.events[0].raw.get("primary_description"))

    def test_routine_6k_cannot_absorb_nearby_true_structural_action(self):
        sec = sources.SourceResult("SEC", "BB", "ok", [
            sources.Event(
                "BB", "filing", "SEC", ex_date="2026-09-01",
                note="6-K · 外国发行人普通备案",
                raw={"relevant": False, "url": "https://sec.example/routine"},
            ),
        ])
        alpaca = sources.SourceResult("Alpaca", "BB", "ok", [
            sources.Event(
                "BB", "filing", "Alpaca", ex_date="2026-09-04",
                note="Alpaca · cash_merger",
                raw={"relevant": True},
            ),
        ])
        groups = [
            group for group in R.reconcile_ticker([sec, alpaca])
            if group.etype == "filing"
        ]

        self.assertEqual(2, len(groups))
        by_date = {group.anchor_date: group for group in groups}
        self.assertEqual({"SEC"}, set(by_date["2026-09-01"].by_source))
        self.assertEqual({"Alpaca"}, set(by_date["2026-09-04"].by_source))
        self.assertEqual(
            "", (by_date["2026-09-04"].by_source.get("SEC") or {}).get("url", ""),
        )

    def test_suspected_6k_cannot_merge_with_different_nearby_action(self):
        sec = sources.SourceResult("SEC", "BB", "ok", [
            sources.Event(
                "BB", "filing", "SEC", ex_date="2026-09-01",
                note="6-K · 疑似公司行动（分红，待核实）",
                raw={"relevant": None, "url": "https://sec.example/dividend-hint"},
            ),
        ])
        alpaca = sources.SourceResult("Alpaca", "BB", "ok", [
            sources.Event(
                "BB", "filing", "Alpaca", ex_date="2026-09-04",
                note="Alpaca · name_changes",
                raw={"relevant": True},
            ),
        ])
        groups = [
            group for group in R.reconcile_ticker([sec, alpaca])
            if group.etype == "filing"
        ]

        self.assertEqual(2, len(groups))
        by_date = {group.anchor_date: group for group in groups}
        self.assertEqual({"SEC"}, set(by_date["2026-09-01"].by_source))
        self.assertEqual({"Alpaca"}, set(by_date["2026-09-04"].by_source))
        self.assertIn("分红", by_date["2026-09-01"].note)
        self.assertIn("name_changes", by_date["2026-09-04"].note)

    def test_suspected_6k_is_verification_not_execution(self):
        group = SimpleNamespace(
            ticker="TSM",
            etype="filing",
            note="6-K · 疑似公司行动（分红，待核实）",
            by_source={"SEC": {
                "note": "6-K · 疑似公司行动（分红，待核实）",
                "relevant": None,
            }},
            conflicts=[],
        )
        # 同时覆盖现货+合约，锁住旧的“现货优先→execution”误升级路径。
        with (
            mock.patch.object(C, "SPOT_TICKERS", {"TSM"}),
            mock.patch.object(C, "CONTRACT_TICKERS", {"TSM"}),
        ):
            decision = run.attach_product_action(
                group, reference_price=None, today=dt.date(2026, 9, 3), forecast=False,
            )

        self.assertEqual("review", decision["status"])
        self.assertEqual("verification", group.follow_up_mode)
        self.assertTrue(any("待核实" in line for line in group.risk))
        self.assertFalse(any("评估下架" in line for line in group.risk))

        reminder = run.schedule_event_reminder({
            "ticker": "TSM",
            "etype": "filing",
            "date": "2026-09-10",
            "days": 7,
            "follow_up_mode": group.follow_up_mode,
            "reminder_state_suffix": group.reminder_state_suffix,
            "filing_relevant": group.filing_relevant,
        }, "TSM|filing|2026-09-10|6k", {}, "2026-09-03")
        self.assertTrue(reminder["verification"])
        self.assertIn("公司行动条款核验", reminder["ops"])
        self.assertNotIn("合约门槛", reminder["ops"])
        self.assertNotIn("执行催办", reminder["ops"])

    def test_confirmed_filing_is_not_suppressed_by_prior_spot_verification(self):
        signature = "BB|filing|2026-09-10|6k"
        fired = {}
        group = SimpleNamespace(
            ticker="BB", etype="filing",
            note="6-K · 疑似公司行动（并购，待核实）",
            by_source={"SEC": {"relevant": None}}, conflicts=[],
        )
        with mock.patch.object(C, "SPOT_TICKERS", {"BB"}):
            run.attach_product_action(
                group, reference_price=None,
                today=dt.date(2026, 9, 3), forecast=False,
            )
        first = run.schedule_event_reminder({
            "ticker": "BB", "etype": "filing", "date": "2026-09-10",
            "days": 7, "follow_up_mode": group.follow_up_mode,
            "verification_kind": group.verification_kind,
            "reminder_state_suffix": group.reminder_state_suffix,
            "filing_relevant": group.filing_relevant,
        }, signature, fired, "2026-09-03")
        self.assertIsNotNone(first)
        self.assertIn(f"{signature}#filing-review", fired)

        confirmed = run.schedule_event_reminder({
            "ticker": "BB", "etype": "filing", "date": "2026-09-10",
            "days": 7, "follow_up_mode": "execution",
            "verification_kind": "", "reminder_state_suffix": "",
            "filing_relevant": True,
        }, signature, fired, "2026-09-03")
        self.assertIsNotNone(confirmed)
        self.assertFalse(confirmed["verification"])
        self.assertIn(signature, fired)


if __name__ == "__main__":
    unittest.main()
