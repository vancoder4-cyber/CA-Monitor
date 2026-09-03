import datetime as dt
import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bot"))

import config as C
import reconcile
import run
import cards

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


class SecFilingClassificationTests(unittest.TestCase):
    def test_only_unambiguous_structural_8k_items_enter_company_action_flow(self):
        for item in ("1.03", "2.01", "3.01", "3.03", "5.01"):
            with self.subTest(item=item):
                _descriptions, relevant = C.describe_8k(item)
                self.assertTrue(relevant)

        # 这些 Item 都可以承载普通财报、融资、投票或任免。
        # 没有解析原文条款前不能冒充并购/退市公司行动。
        for items in (
            "1.01,2.03,9.01",
            "2.02,8.01,9.01",
            "5.02,8.01",
            "5.07,8.01",
            "7.01,8.01",
            "8.01,9.01",
        ):
            with self.subTest(items=items):
                _descriptions, relevant = C.describe_8k(items)
                self.assertFalse(relevant)

        # 一份文件同时含明确结构性 Item 时仍应保留。
        self.assertTrue(C.describe_8k("2.02,3.03,8.01,9.01")[1])

    def test_routine_filing_is_filtered_by_producer_and_calendar_image_guard(self):
        group = SimpleNamespace(etype="filing", filing_relevant=False)
        self.assertTrue(run._is_routine_filing(group))
        self.assertFalse(bot_render._is_visible_event({
            "ticker": "IBM",
            "etype": "filing",
            "filing_relevant": False,
        }))
        self.assertTrue(bot_render._is_visible_event({
            "ticker": "IBM",
            "etype": "filing",
            "filing_relevant": True,
        }))

    def test_build_keeps_routine_filing_only_in_sec_source_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            cache.mkdir()
            (cache / "IBM.json").write_text(json.dumps({
                "ticker": "IBM",
                "fetched": "2026-09-03T12:00:00",
                "health": {"SEC": "ok"},
                "groups": [{
                    "ticker": "IBM",
                    "etype": "filing",
                    "anchor_date": "2026-09-03",
                    "by_source": {"SEC": {
                        "ex_date": "2026-09-03",
                        "url": "https://sec.example/routine",
                        "note": "ordinary-earnings-filing",
                        "relevant": False,
                    }},
                    "sources_ok": ["SEC"],
                    "status": "conflict",
                    "conflicts": ["must-not-count"],
                    "gaps": ["must-not-count"],
                    "note": "ordinary-earnings-filing",
                }],
            }), encoding="utf-8")
            state = root / "state.json"
            site_data = root / "site_data.json"
            dashboard = root / "dashboard.html"
            digest = root / "digest.txt"
            patches = (
                mock.patch.object(run, "CACHE", str(cache)),
                mock.patch.object(run, "STATE_PATH", str(state)),
                mock.patch.object(run, "FORECAST_WATCH_PATH", str(root / "forecast_watch.json")),
                mock.patch.object(run, "OUT_HTML", str(dashboard)),
                mock.patch.object(run, "OUT_DIGEST", str(digest)),
                mock.patch.object(run, "OUT_SITEDATA", str(site_data)),
                mock.patch.object(run, "business_today", return_value=dt.date(2026, 9, 3)),
                mock.patch.object(reconcile, "business_today", return_value=dt.date(2026, 9, 3)),
                mock.patch.object(run, "load_refs", return_value={"ir_dividend": {}}),
                mock.patch.object(run, "load_acknowledged", return_value=[]),
                mock.patch.object(run, "load_forecast_watches", return_value=[]),
                mock.patch.object(run.notify_lark, "notify", return_value=(False, "test skip")),
                mock.patch.object(C, "TICKERS", ["IBM"]),
                mock.patch.object(C, "ALL_ASSETS", ["IBM"]),
                mock.patch.object(C, "SPOT_TICKERS", {"IBM"}),
                mock.patch.object(C, "CONTRACT_TICKERS", {"IBM"}),
            )
            with ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)
                with redirect_stdout(io.StringIO()):
                    alerts = run.build()

            data = json.loads(site_data.read_text(encoding="utf-8"))
            page = dashboard.read_text(encoding="utf-8")
            text_digest = digest.read_text(encoding="utf-8")
            for key in ("new", "rounds", "conflicts", "gaps", "pending", "announced"):
                self.assertEqual([], alerts[key], key)
            for key in ("new", "conflicts", "gaps", "calendar"):
                self.assertEqual([], data[key], key)
            self.assertEqual(0, data["counts"]["new"])
            self.assertEqual(0, data["counts"]["conflicts"])
            self.assertEqual(0, data["counts"]["gaps"])
            # 可审计的 SEC 原文仍留在网站，但不带 CA 风控。
            self.assertIn("ordinary-earnings-filing", page)
            self.assertNotIn("现货：评估下架/暂停充提与交易", page)
            self.assertNotIn("ordinary-earnings-filing", text_digest)

    def test_qa_assistant_never_surfaces_routine_filing_from_legacy_snapshot(self):
        routine = {
            "ticker": "IBM",
            "etype": "filing",
            "date": "2026-09-03",
            "days": 0,
            "event_id": "IBM|filing|2026-09-03|routine",
            "filing_relevant": False,
            "note": "ordinary-earnings-filing",
            "products": ["现货", "合约"],
            "risk": ["现货：评估下架/暂停交易"],
            "contract_action": {"status": "not_required"},
            "follow_up_mode": "none",
            "forecast": True,
            "decl": "2026-09-03",
            "record": None,
            "pay": None,
            "srcs": ["SEC"],
            "conflicts": ["must-not-render"],
            "gaps": ["must-not-render"],
        }
        data = {
            "generated": "test",
            "business_date": "2026-09-03",
            "counts": {
                "pending": 1,
                "forecasts": 1,
                "new": 1,
                "conflicts": 1,
                "gaps": 1,
                "announced": 1,
            },
            "coverage": [{
                "ticker": "IBM",
                "name": "IBM",
                "spot": True,
                "contract": True,
                "monitored": True,
                "type_cn": "个股",
            }],
            "pending": [routine],
            "forecasts": [routine],
            "new": [routine],
            "conflicts": [routine],
            "gaps": [routine],
            "announced": [routine],
            "recent_declares": [routine],
            "calendar": [routine],
            "refs": {},
        }

        rendered = [
            cards.calendar_card(data, ""),
            cards.alert_card(data, ""),
            cards.risk_card(data, ""),
            cards.today_card(data, ""),
            cards.week_card(data, ""),
            cards.upcoming_card(data, ""),
            cards.forecast_card(data, ""),
            cards.announce_card(data, ""),
            cards.lookup_card(data, "IBM", ""),
        ]
        text = json.dumps(rendered, ensure_ascii=False)
        self.assertNotIn("ordinary-earnings-filing", text)
        self.assertNotIn("must-not-render", text)
        self.assertNotIn("暂停交易", text)
        self.assertIn("当前无风控事项", text)
        self.assertIn("当前没有待核实预测", text)


if __name__ == "__main__":
    unittest.main()
