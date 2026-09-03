# -*- coding: utf-8 -*-
import copy
import datetime as dt
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import report
import run
import config as C

# The production Bot image installs Pillow, while the lightweight repository
# test environment intentionally does not.  _label() is pure formatting code,
# so provide import-only stubs instead of making this unit test depend on the
# image-rendering runtime.
if "PIL" not in sys.modules:
    pil = types.ModuleType("PIL")
    pil.Image = types.SimpleNamespace()
    pil.ImageDraw = types.SimpleNamespace()
    pil.ImageFont = types.SimpleNamespace()
    sys.modules["PIL"] = pil
from bot import render
from bot import cards as bot_cards
from tools.validate_public_snapshot import validate as validate_public
from tools.validate_state import validate as validate_state


def _public_snapshot():
    return {
        "schema_version": 4,
        "generated": "fixture",
        "generated_at_utc": "2026-09-03T13:35:00Z",
        "valid_until_utc": "2026-09-03T20:45:00Z",
        "business_date": "2026-09-03",
        "source_sha": "abc123",
        "run_id": "99",
        "delivery_status": "sent",
        "counts": {},
        "coverage": [], "pending": [], "forecasts": [], "calendar": [],
        "announced": [], "recent_declares": [], "conflicts": [], "gaps": [],
        "filing_updates": [],
        "changelog": [{"head": "2026-09-03 · fixture", "items": []}],
    }


class PublicSnapshotTests(unittest.TestCase):
    def test_producer_and_bot_schema_versions_match(self):
        self.assertEqual(C.PUBLIC_DATA_SCHEMA_VERSION, bot_cards.PUBLIC_DATA_SCHEMA_VERSION)

    def test_public_snapshot_accepts_matching_provenance(self):
        validate_public(_public_snapshot(), expected_sha="abc123", expected_run_id="99")

    def test_public_snapshot_rejects_actor_keys_and_open_ids_recursively(self):
        for payload in (
            {"resolved": [{"by": "hidden"}]},
            {"resolved": [{"note": "owner ou_notpublic"}]},
        ):
            with self.subTest(payload=payload):
                data = _public_snapshot()
                data.update(payload)
                with self.assertRaises(ValueError):
                    validate_public(data)

    def test_public_snapshot_rejects_inverted_validity_window(self):
        data = _public_snapshot()
        data["valid_until_utc"] = data["generated_at_utc"]
        with self.assertRaisesRegex(ValueError, "later than"):
            validate_public(data)

    def test_public_snapshot_rejects_missing_bot_contract_metadata(self):
        for key in ("generated", "business_date", "source_sha"):
            with self.subTest(key=key):
                data = _public_snapshot()
                data.pop(key)
                with self.assertRaisesRegex(ValueError, key):
                    validate_public(data)

    def test_site_exposes_commit_schema_and_stale_guard(self):
        meta = {
            "generated": "fixture", "business_date": "2026-09-03",
            "generated_at_utc": "2026-09-03T13:35:00Z",
            "valid_until_utc": "2026-09-03T20:45:00Z",
            "source_sha": "abcdef1234567890", "schema_version": 4,
            "run_url": "https://github.com/example/repo/actions/runs/99",
        }
        page = report._site_shell(meta, "dash", "calendar", "log")
        self.assertIn("schema v4", page)
        self.assertIn("abcdef123456", page)
        self.assertIn('data-valid-until="2026-09-03T20:45:00Z"', page)
        self.assertIn("validUntil<=generated", page)
        self.assertIn("10*60*1000", page)
        self.assertIn("请勿据此判断", page)


class StateGuardTests(unittest.TestCase):
    def test_state_validator_fails_closed_on_missing_history(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            path.write_text(json.dumps({"seen": {}, "fired_rounds": {}, "declared": {}}))
            with self.assertRaises(ValueError):
                validate_state(path)

    def test_state_validator_accepts_nonempty_history(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            path.write_text(json.dumps({
                "seen": {"AAPL|dividend|2026-09-01": "2026-08-01"},
                "fired_rounds": {}, "declared": {},
            }))
            validate_state(path)


class EventIdentityTests(unittest.TestCase):
    def test_sec_filing_id_does_not_change_when_vendor_source_appears(self):
        base = SimpleNamespace(
            ticker="IBM", etype="filing", anchor_date="2026-09-03",
            note="8-K · 重大事项",
            by_source={"SEC": {"url": "https://www.sec.gov/filing-a", "form": "8-K"}},
        )
        enriched = copy.deepcopy(base)
        enriched.by_source["Alpaca"] = {"url": "", "form": "", "relevant": True}
        self.assertEqual(run.sig(base), run.sig(enriched))

        other = copy.deepcopy(base)
        other.by_source["SEC"]["url"] = "https://www.sec.gov/filing-b"
        self.assertNotEqual(run.sig(base), run.sig(other))


class CalendarImageTests(unittest.TestCase):
    def test_png_label_prioritizes_product_action_status(self):
        event = {
            "ticker": "IBM", "etype": "dividend", "products": ["现货", "合约"],
            "value_verified": True,
            "contract_action": {"status": "not_required"},
        }
        self.assertIn("现货处理·合约无需", render._label(event))
        event["contract_action"] = {"status": "required"}
        self.assertIn("合约需操作", render._label(event))


class ProvenanceTests(unittest.TestCase):
    def test_provenance_contains_a_bounded_validity_window(self):
        with mock.patch.dict(os.environ, {
            "GITHUB_SHA": "abc123", "GITHUB_REPOSITORY": "example/repo",
            "GITHUB_RUN_ID": "99",
        }):
            meta = run._provenance_meta()
        generated = dt.datetime.fromisoformat(meta["generated_at_utc"].replace("Z", "+00:00"))
        valid_until = dt.datetime.fromisoformat(meta["valid_until_utc"].replace("Z", "+00:00"))
        self.assertGreater(valid_until, generated)
        self.assertEqual("abc123", meta["source_sha"])
        self.assertTrue(meta["run_url"].endswith("/99"))

    def test_weekend_push_remains_valid_until_the_next_weekday_scan(self):
        saturday = dt.datetime(2026, 9, 5, 14, 0, tzinfo=dt.timezone.utc)

        meta = run._provenance_meta(saturday)

        self.assertEqual("2026-09-05T14:00:00Z", meta["generated_at_utc"])
        # Monday 09:35 ET + the documented four-hour queue allowance.
        self.assertEqual("2026-09-07T17:35:00Z", meta["valid_until_utc"])

    def test_friday_after_close_validity_advances_to_monday_scan_window(self):
        friday_after_close = dt.datetime(
            2026, 9, 11, 21, 0, tzinfo=dt.timezone.utc
        )
        meta = run._provenance_meta(friday_after_close)

        # Friday 17:00 ET is after the last scan.  The next scheduled scan is
        # Monday 09:35 ET (13:35Z), plus the documented four-hour grace.
        self.assertEqual("2026-09-11T21:00:00Z", meta["generated_at_utc"])
        self.assertEqual("2026-09-14T17:35:00Z", meta["valid_until_utc"])


if __name__ == "__main__":
    unittest.main()
