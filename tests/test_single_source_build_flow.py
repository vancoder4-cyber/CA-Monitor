import datetime as dt
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest import mock

import reconcile
import run


BUSINESS_DAY = dt.date(2026, 8, 31)
EVENT_DATE = "2026-09-03"
SIGNATURE = f"HD|dividend|{EVENT_DATE}"


def _group(sources):
    fields = {
        "ex_date": EVENT_DATE,
        "declaration_date": None,
        "record_date": EVENT_DATE,
        "pay_date": "2026-09-17",
        "amount": 2.33,
    }
    return {
        "ticker": "HD",
        "etype": "dividend",
        "anchor_date": EVENT_DATE,
        "by_source": {source: dict(fields) for source in sources},
        "sources_ok": list(sources),
        "status": "single" if len(sources) == 1 else "confirmed",
        "conflicts": [],
        "gaps": [],
        "note": "",
    }


class SingleSourceBuildFlowTests(unittest.TestCase):
    def _write_cache(self, cache_dir, sources):
        payload = {
            "ticker": "HD",
            "fetched": "2026-08-31T12:00:00",
            "health": {source: "ok" for source in sources},
            "groups": [_group(sources)],
        }
        (cache_dir / "HD.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def test_build_pushes_single_source_then_announces_same_day_promotion(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            cache = root / "cache"
            cache.mkdir()
            state = root / "state.json"
            forecast_watch = root / "forecast_watch.json"
            dashboard = root / "dashboard.html"
            digest = root / "digest.txt"
            site_data = root / "site_data.json"

            patches = (
                mock.patch.object(run, "CACHE", str(cache)),
                mock.patch.object(run, "STATE_PATH", str(state)),
                mock.patch.object(run, "FORECAST_WATCH_PATH", str(forecast_watch)),
                mock.patch.object(run, "OUT_HTML", str(dashboard)),
                mock.patch.object(run, "OUT_DIGEST", str(digest)),
                mock.patch.object(run, "OUT_SITEDATA", str(site_data)),
                mock.patch.object(run, "business_today", return_value=BUSINESS_DAY),
                mock.patch.object(reconcile, "business_today", return_value=BUSINESS_DAY),
                mock.patch.object(run, "load_refs", return_value={"ir_dividend": {}}),
                mock.patch.object(run, "load_acknowledged", return_value=[]),
                mock.patch.object(run, "load_forecast_watches", return_value=[]),
                mock.patch.object(run.notify_lark, "notify", return_value=(False, "test skip")),
                mock.patch.object(run.C, "TICKERS", ["HD"]),
                mock.patch.object(run.C, "ALL_ASSETS", ["HD"]),
                mock.patch.object(run.C, "SPOT_TICKERS", {"HD"}),
                mock.patch.object(run.C, "CONTRACT_TICKERS", set()),
            )

            with ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)
                self._write_cache(cache, ["Alpaca"])
                with redirect_stdout(io.StringIO()):
                    first = run.build()

                self.assertEqual([], first["pending"])
                self.assertEqual(1, len(first["forecasts"]))
                self.assertEqual(1, len(first["rounds"]))
                self.assertTrue(first["rounds"][0]["forecast"])
                self.assertIn("勿执行", first["rounds"][0]["ops"])

                first_state = json.loads(state.read_text(encoding="utf-8"))
                self.assertEqual("watching", first_state["forecast_status"][SIGNATURE]["status"])
                self.assertIn(f"{SIGNATURE}#verification", first_state["fired_rounds"])
                self.assertNotIn(SIGNATURE, first_state["fired_rounds"])

                self._write_cache(cache, ["Alpaca", "Tiingo"])
                with redirect_stdout(io.StringIO()):
                    second = run.build()

                self.assertEqual([], second["forecasts"])
                self.assertEqual(1, len(second["pending"]))
                self.assertEqual(1, len(second["rounds"]))
                self.assertFalse(second["rounds"][0]["forecast"])
                self.assertTrue(second["rounds"][0]["promoted_from_forecast"])
                self.assertEqual(1, len(second["forecast_updates"]))

                second_state = json.loads(state.read_text(encoding="utf-8"))
                self.assertEqual("confirmed", second_state["forecast_status"][SIGNATURE]["status"])
                self.assertIn(SIGNATURE, second_state["fired_rounds"])

                # 已逐项核验的一手 CompanyIR 即使未提供 declaration_date，
                # 也必须保持正式，不能重新降为普通单源预测。
                self._write_cache(cache, ["CompanyIR"])
                with redirect_stdout(io.StringIO()):
                    official_only = run.build()
                self.assertEqual([], official_only["forecasts"])
                self.assertEqual(1, len(official_only["pending"]))
                self.assertTrue(official_only["pending"][0]["official"])


if __name__ == "__main__":
    unittest.main()
