import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import reconcile as R
import run


EXPECTED = {
    "HD": {
        "anchor_date": "2026-09-03",
        "declaration_date": "2026-08-20",
        "record_date": "2026-09-03",
        "pay_date": "2026-09-17",
        "amount": 2.33,
        "url": "https://ir.homedepot.com/news-releases/2026/08-20-2026-211013830",
    },
    "QCOM": {
        "anchor_date": "2026-09-03",
        "declaration_date": "2026-07-17",
        "record_date": "2026-09-03",
        "pay_date": "2026-09-24",
        "amount": 0.92,
        "url": "https://www.qualcomm.com/news/releases/2026/07/qualcomm-announces-quarterly-cash-dividend",
    },
    "GOOGL": {
        "anchor_date": "2026-09-04",
        "declaration_date": "2026-07-22",
        "record_date": "2026-09-07",
        "pay_date": "2026-09-14",
        "amount": 0.22,
        "url": "https://www.sec.gov/Archives/edgar/data/1652044/000165204426000066/googexhibit991q22026.htm",
    },
    "TER": {
        "anchor_date": "2026-09-04",
        "declaration_date": "2026-08-24",
        "record_date": "2026-09-04",
        "pay_date": "2026-09-25",
        "amount": 0.13,
        "url": "https://investors.teradyne.com/news-events/press-releases/detail/446/teradyne-declares-quarterly-cash-dividend",
    },
    "WDC": {
        "anchor_date": "2026-09-08",
        "declaration_date": "2026-08-04",
        "record_date": "2026-09-08",
        "pay_date": "2026-09-17",
        "amount": 0.15,
        "url": "https://investor.wdc.com/static-files/fad0b624-3d57-4fc3-8bbf-06b0e9b19348",
    },
    "NVDA": {
        "anchor_date": "2026-09-10",
        "declaration_date": "2026-08-26",
        "record_date": "2026-09-10",
        "pay_date": "2026-10-01",
        "amount": 0.25,
        "url": "https://nvidianews.nvidia.com/news/nvidia-announces-financial-results-for-second-quarter-fiscal-2027",
    },
}


class OfficialEventOverrideTests(unittest.TestCase):
    def test_verified_official_dividends_load_as_confirmed_events(self):
        refs = run.load_refs()
        groups = {}

        run.apply_official_event_overrides(
            groups,
            refs,
            allowed_tickers=set(EXPECTED),
        )

        for ticker, expected in EXPECTED.items():
            with self.subTest(ticker=ticker):
                self.assertEqual(1, len(groups[ticker]))
                group = groups[ticker][0]
                self.assertEqual("dividend", group.etype)
                self.assertEqual(expected["anchor_date"], group.anchor_date)
                self.assertEqual("confirmed", group.status)
                self.assertFalse(R.is_disputed(group))

                official = group.by_source["CompanyIR"]
                for field in (
                    "declaration_date",
                    "record_date",
                    "pay_date",
                    "amount",
                    "url",
                ):
                    self.assertEqual(expected[field], official[field])

    def test_qcom_declaration_date_is_corrected_to_official_release_date(self):
        refs = run.load_refs()
        override = refs["official_event_overrides"][
            "QCOM|dividend|2026-09-03"
        ]

        self.assertEqual("2026-07-17", override["declaration_date"])
        self.assertNotEqual("2026-07-09", override["declaration_date"])

    def test_qcom_official_declaration_wins_over_stale_vendor_date(self):
        refs = run.load_refs()
        qcom_key = "QCOM|dividend|2026-09-03"
        refs = {
            **refs,
            "official_event_overrides": {
                qcom_key: refs["official_event_overrides"][qcom_key]
            },
        }
        qcom = R.EventGroup(
            ticker="QCOM",
            etype="dividend",
            anchor_date="2026-09-03",
            by_source={
                "Alpaca": {
                    "ex_date": "2026-09-03",
                    "declaration_date": "2026-07-09",
                    "record_date": "2026-09-03",
                    "pay_date": "2026-09-24",
                    "amount": 0.92,
                }
            },
            sources_ok=["Alpaca"],
        )
        R.evaluate_group(qcom)

        run.apply_official_event_overrides(
            {"QCOM": [qcom]},
            refs,
            allowed_tickers={"QCOM"},
        )

        self.assertEqual(
            "2026-07-17",
            R.pick_value(qcom.by_source, "declaration_date"),
        )
        self.assertEqual("confirmed", qcom.status)
        self.assertFalse(R.is_disputed(qcom))


if __name__ == "__main__":
    unittest.main()
