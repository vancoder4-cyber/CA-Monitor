import datetime as dt
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as C
import report


def _empty_alerts():
    return {
        "new": [],
        "rounds": [],
        "conflicts": [],
        "gaps": [],
        "pending": [],
        "announced": [],
        "resolved": [],
        "forecasts": [],
        "forecast_updates": [],
        "contract_updates": [],
        "review": {
            "open": 0,
            "overdue": 0,
            "max_age": 0,
            "escalate_days": 3,
            "conflicts": 0,
            "gaps": 0,
        },
    }


def _event(ticker, etype, date, event_id, **extra):
    event = {
        "ticker": ticker,
        "etype": etype,
        "date": date,
        "event_id": event_id,
        "days": 7,
        "products": [],
        "risk": [],
        "record": None,
        "pay": None,
        "decl": None,
        "srcs": ["SEC"],
    }
    event.update(extra)
    return event


class ReportSurfaceConsistencyTests(unittest.TestCase):
    def test_routine_filing_stays_in_sec_audit_table_only(self):
        sentinel = "routine-filing-must-stay-out-of-business-sections"
        group = SimpleNamespace(
            ticker="IBM",
            etype="filing",
            anchor_date="2026-09-03",
            event_id="IBM|filing|2026-09-03|routine",
            filing_relevant=False,
            by_source={"SEC": {
                "url": "https://www.sec.gov/example/routine",
                "accepted": "2026-09-03 08:00 ET",
                "relevant": False,
            }},
            status="conflict",
            conflicts=[sentinel],
            gaps=[sentinel],
            note=sentinel,
            risk=[sentinel],
            age_days=99,
            is_future=True,
            days_to=0,
        )
        routine = _event(
            "IBM", "filing", "2026-09-03", group.event_id,
            filing_relevant=False,
            note=sentinel,
            forecast=True,
            follow_up_mode="execution",
            ops=sentinel,
            kind="updated",
            current_status="required",
        )
        alerts = _empty_alerts()
        alerts.update({
            "new": [group],
            "rounds": [routine],
            "conflicts": [group],
            "gaps": [group],
            "pending": [routine],
            "announced": [{**routine, "decl": "2026-09-03"}],
            "resolved": [{**routine, "detail": sentinel}],
            "forecasts": [routine],
            "forecast_updates": [routine],
            "contract_updates": [routine],
            # 模拟旧快照残留的幽灵汇总；报告必须按过滤后的明细重算。
            "review": {
                "open": 2,
                "overdue": 2,
                "max_age": 99,
                "escalate_days": 3,
                "conflicts": 1,
                "gaps": 1,
            },
        })
        meta = {"business_date": "2026-09-03", "generated": "test"}

        with mock.patch.object(C, "TICKERS", []), mock.patch.object(C, "ALL_ASSETS", []):
            dashboard = report.build_dashboard(
                {"IBM": [group]}, {}, alerts, meta,
            )
        business, sec_audit = dashboard.split("📄 SEC 原文", 1)
        digest = report.build_text_digest(alerts, meta)

        self.assertNotIn(sentinel, business)
        self.assertNotIn("待人工确认 2 条", business)
        self.assertNotIn(sentinel, digest)
        self.assertIn("字段冲突(零容忍·需人工确认) (0)", digest)
        self.assertIn("数据空缺(需人工确认) (0)", digest)

        self.assertIn("近 90 天：普通备案 + 公司行动相关文件", sec_audit)
        self.assertIn(sentinel, sec_audit)
        self.assertIn("标为「公司行动相关」或「疑似相关」的项目进入公司行动流", sec_audit)
        self.assertIn("标为「一般」的普通备案不进入", sec_audit)

    def test_unknown_filing_is_not_removed_by_report_boundary(self):
        unknown = _event(
            "IBM", "filing", "2026-09-03", "IBM|filing|2026-09-03|unknown",
            filing_relevant=None,
        )
        relevant = {**unknown, "filing_relevant": True}
        routine = {**unknown, "filing_relevant": False}
        self.assertEqual([unknown, relevant], report._non_routine([unknown, routine, relevant]))

    def test_suspected_6k_is_visibly_marked_for_review_in_sec_table(self):
        group = SimpleNamespace(
            ticker="TSM",
            etype="filing",
            anchor_date="2026-09-01",
            event_id="TSM|filing|2026-09-01|6k-dividend",
            filing_relevant=None,
            by_source={"SEC": {
                "url": "https://www.sec.gov/example/6k",
                "accepted": "2026-09-01 08:00 ET",
                "relevant": None,
            }},
            status="confirmed",
            conflicts=[],
            gaps=[],
            note="6-K · 疑似公司行动（分红，待核实）",
            risk=[],
            is_future=False,
            days_to=-2,
        )
        with mock.patch.object(C, "TICKERS", []), mock.patch.object(C, "ALL_ASSETS", []):
            dashboard = report.build_dashboard(
                {"TSM": [group]}, {}, _empty_alerts(),
                {"business_date": "2026-09-03", "generated": "test"},
            )
        self.assertIn("疑似相关 · 待核实", dashboard)
        self.assertIn("6-K · 疑似公司行动", dashboard)

    def test_split_uses_effective_date_wording_across_report_and_digest(self):
        split_group = SimpleNamespace(
            ticker="SPLT",
            etype="split",
            anchor_date="2026-09-10",
            by_source={"CompanyIR": {
                "declaration_date": "2026-09-01",
                "record_date": "2026-09-08",
                "pay_date": None,
                "ratio": "2:1",
            }},
            first_announced="2026-09-01",
            selected_amount=None,
            selected_ratio="2:1",
            value_display="2:1",
            value_verified=True,
            conflicts=[],
            risk=[],
            forecast=False,
            status="confirmed",
            primary_url="",
        )
        key_dates = report._fmt_key_dates(split_group)
        marks = report._collect_calendar_marks(
            {"SPLT": [split_group]},
            dt.date(2026, 9, 1),
            dt.date(2026, 9, 30),
        )
        self.assertIn("生效", key_dates)
        self.assertNotIn("除息", key_dates)
        self.assertIn("生效 2026-09-10", marks["2026-09-10"][0]["tip"])

        alerts = _empty_alerts()
        alerts["new"] = [split_group]
        alerts["announced"] = [_event(
            "NEWSP", "split", "2026-09-10", "NEWSP|split|2026-09-10",
            decl="2026-09-01",
        )]
        alerts["pending"] = [_event(
            "PENDSP", "split", "2026-09-11", "PENDSP|split|2026-09-11",
            days=8,
        )]
        meta = {"business_date": "2026-09-03", "generated": "test"}
        with mock.patch.object(C, "TICKERS", []), mock.patch.object(C, "ALL_ASSETS", []):
            dashboard = report.build_dashboard({}, {}, alerts, meta)
        digest = report.build_text_digest(alerts, meta)

        self.assertIn("宣告 2026-09-01</span> · 生效 2026-09-10", dashboard)
        self.assertIn("生效 2026-09-11", dashboard)
        self.assertIn("SPLT</b> 拆股 生效 2026-09-10", dashboard)
        self.assertIn("NEWSP 拆股 宣告 2026-09-01 · 生效 2026-09-10", digest)
        self.assertIn("PENDSP 拆股 还剩8天 · 生效 2026-09-11", digest)
        self.assertIn("SPLT 拆股 生效 2026-09-10", digest)
        self.assertNotIn("NEWSP 拆股 宣告 2026-09-01 · 除息", digest)
        self.assertNotIn("PENDSP 拆股 还剩8天 · 除息", digest)


if __name__ == "__main__":
    unittest.main()
