import datetime as dt
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


SEC_URL = "https://www.sec.gov/Archives/edgar/data/1/0001/report6k.htm"
EVENT_DATE = "2026-09-03"


def _group(relevant):
    return {
        "ticker": "IBM",
        "etype": "filing",
        "anchor_date": EVENT_DATE,
        "by_source": {"SEC": {
            "ex_date": EVENT_DATE,
            "url": SEC_URL,
            "note": "6-K · suspected dividend terms",
            "relevant": relevant,
        }},
        "sources_ok": ["SEC"],
        "status": "single",
        "conflicts": [],
        "gaps": [],
        "note": "6-K · suspected dividend terms",
    }


class FilingRelevanceTransitionTests(unittest.TestCase):
    def _scenario(self, final_relevance):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        cache = root / "cache"
        cache.mkdir()
        paths = {
            "state": root / "state.json",
            "site": root / "site_data.json",
            "page": root / "dashboard.html",
            "digest": root / "digest.txt",
        }
        current_day = [dt.date(2026, 9, 3)]

        def write_cache(relevant):
            (cache / "IBM.json").write_text(json.dumps({
                "ticker": "IBM",
                "fetched": f"{current_day[0]}T12:00:00",
                "health": {"SEC": "ok"},
                "groups": [_group(relevant)],
            }), encoding="utf-8")

        stack = ExitStack()
        patches = (
            mock.patch.object(run, "CACHE", str(cache)),
            mock.patch.object(run, "STATE_PATH", str(paths["state"])),
            mock.patch.object(run, "FORECAST_WATCH_PATH", str(root / "forecast_watch.json")),
            mock.patch.object(run, "OUT_HTML", str(paths["page"])),
            mock.patch.object(run, "OUT_DIGEST", str(paths["digest"])),
            mock.patch.object(run, "OUT_SITEDATA", str(paths["site"])),
            mock.patch.object(run, "business_today", side_effect=lambda: current_day[0]),
            mock.patch.object(reconcile, "business_today", side_effect=lambda: current_day[0]),
            mock.patch.object(run, "load_refs", return_value={"ir_dividend": {}}),
            mock.patch.object(run, "load_acknowledged", return_value=[]),
            mock.patch.object(run, "load_forecast_watches", return_value=[]),
            mock.patch.object(run.notify_lark, "notify", return_value=(False, "test skip")),
            mock.patch.object(C, "TICKERS", ["IBM"]),
            mock.patch.object(C, "ALL_ASSETS", ["IBM"]),
            mock.patch.object(C, "SPOT_TICKERS", {"IBM"}),
            mock.patch.object(C, "CONTRACT_TICKERS", set()),
        )
        for patcher in patches:
            stack.enter_context(patcher)
        self.addCleanup(stack.close)
        self.addCleanup(temp.cleanup)
        return current_day, write_cache, paths

    def test_d0_candidate_then_d1_confirmed_emits_once_even_after_anchor(self):
        current_day, write_cache, paths = self._scenario(True)
        write_cache(None)
        with redirect_stdout(io.StringIO()):
            first = run.build()

        self.assertEqual([], first["filing_updates"])
        self.assertEqual(1, len(first["rounds"]))
        event_id = first["rounds"][0]["event_id"]
        state = json.loads(paths["state"].read_text(encoding="utf-8"))
        self.assertEqual("review", state["filing_relevance_status"][event_id]["status"])

        current_day[0] = dt.date(2026, 9, 4)
        write_cache(True)
        with redirect_stdout(io.StringIO()):
            second = run.build()

        self.assertEqual([], second["rounds"])
        self.assertEqual([], second["new"])
        self.assertEqual(1, len(second["filing_updates"]))
        update = second["filing_updates"][0]
        self.assertEqual("confirmed", update["kind"])
        self.assertEqual("execution", update["follow_up_mode"])
        self.assertEqual(SEC_URL, update["sec_url"])
        self.assertIn("suspected dividend terms", update["note"])

        published = json.loads(paths["site"].read_text(encoding="utf-8"))
        self.assertEqual([update], published["filing_updates"])
        self.assertIn("条款核验已确认", paths["page"].read_text(encoding="utf-8"))
        self.assertIn("条款核验已确认", paths["digest"].read_text(encoding="utf-8"))
        self.assertIn(SEC_URL, paths["digest"].read_text(encoding="utf-8"))

        with mock.patch.object(notify_lark, "_load_mentions", return_value=["ou_owner"]):
            card = notify_lark._build_card(second, {"generated": "test"})
        lark_text = json.dumps(card, ensure_ascii=False)
        self.assertIn("条款核验已确认", lark_text)
        self.assertIn("<at id=ou_owner></at>", lark_text)
        self.assertIn(SEC_URL, lark_text)

        bot_text = json.dumps(cards.alert_card(published, ""), ensure_ascii=False)
        self.assertIn("条款核验已确认", bot_text)
        self.assertIn(SEC_URL, bot_text)

        # 同一最终状态再次构建不得重发迁移事件。
        with redirect_stdout(io.StringIO()):
            third = run.build()
        self.assertEqual([], third["filing_updates"])

    def test_past_candidate_repeats_daily_review_until_routine_resolution(self):
        current_day, write_cache, paths = self._scenario(False)
        write_cache(None)
        with redirect_stdout(io.StringIO()):
            run.build()

        current_day[0] = dt.date(2026, 9, 4)
        with redirect_stdout(io.StringIO()):
            d1 = run.build()
        self.assertEqual("review_pending", d1["filing_updates"][0]["kind"])
        self.assertEqual(-1, d1["filing_updates"][0]["days"])
        self.assertIn("事件日已过 1 天", paths["digest"].read_text(encoding="utf-8"))
        self.assertIn(SEC_URL, paths["digest"].read_text(encoding="utf-8"))

        # 同一业务日只发一次。
        with redirect_stdout(io.StringIO()):
            same_day = run.build()
        self.assertEqual([], same_day["filing_updates"])

        # 下一业务日仍未解决则再次核验。
        current_day[0] = dt.date(2026, 9, 5)
        with redirect_stdout(io.StringIO()):
            d2 = run.build()
        self.assertEqual("review_pending", d2["filing_updates"][0]["kind"])
        self.assertEqual(-2, d2["filing_updates"][0]["days"])

        # 被判为普通备案后发一次明确解除，且绝不 @ 执行负责人。
        current_day[0] = dt.date(2026, 9, 6)
        write_cache(False)
        with redirect_stdout(io.StringIO()):
            resolved = run.build()
        self.assertEqual(1, len(resolved["filing_updates"]))
        update = resolved["filing_updates"][0]
        self.assertEqual("routine", update["kind"])
        self.assertEqual("none", update["follow_up_mode"])
        with mock.patch.object(notify_lark, "_load_mentions", return_value=["ou_owner"]):
            card = notify_lark._build_card(resolved, {"generated": "test"})
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn("普通备案，不属于公司行动；本次无需操作", text)
        self.assertNotIn("<at id=ou_owner></at>", text)

        with redirect_stdout(io.StringIO()):
            stable = run.build()
        self.assertEqual([], stable["filing_updates"])

    def test_filing_update_has_priority_over_round_and_new_copy(self):
        event = {
            "ticker": "IBM", "etype": "filing", "date": EVENT_DATE,
            "event_id": "IBM|filing|stable", "kind": "confirmed",
            "current_status": "confirmed", "filing_relevant": True,
            "follow_up_mode": "execution", "products": ["现货"],
            "risk": [], "note": "confirmed terms", "sec_url": SEC_URL,
        }
        alerts = {
            "filing_updates": [event], "rounds": [event], "new": [event],
            "forecast_updates": [], "contract_updates": [], "announced": [],
            "conflicts": [], "gaps": [], "forecasts": [], "pending": [],
            "review": {},
        }
        lark_visible = notify_lark._visible_alert_items(alerts)
        report_visible = report._visible_alert_items(alerts)
        for visible in (lark_visible, report_visible):
            self.assertEqual([event], visible["filing_updates"])
            self.assertEqual([], visible["rounds"])
            self.assertEqual([], visible["new"])


if __name__ == "__main__":
    unittest.main()
