import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "monitor.yml"


class WorkflowConfigTests(unittest.TestCase):
    def test_schedule_uses_new_york_timezone_without_a_runtime_gate(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        schedules = ("35 9 * * 1-5", "45 12 * * 1-5", "5 16 * * 1-5")

        self.assertEqual(3, text.count('timezone: "America/New_York"'))
        for schedule in schedules:
            with self.subTest(schedule=schedule):
                entry = (
                    f'- cron: "{schedule}"\n'
                    '      timezone: "America/New_York"'
                )
                self.assertIn(entry, text)
        self.assertNotIn("schedule_gate:", text)

    def test_production_requires_lark_delivery(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('LARK_REQUIRED: "1"', text)
        self.assertIn("LARK_ALERT_MENTION_OPEN_IDS: ${{ secrets.LARK_ALERT_MENTION_OPEN_IDS }}", text)

    def test_dedup_state_cache_is_isolated_from_supporting_data(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        core = text.split("- name: 恢复核心去重状态", 1)[1].split(
            "- name: 迁移新版旧缓存", 1
        )[0]
        support = text.split("- name: 恢复辅助数据缓存", 1)[1].split(
            "- name: 抓取 + 核对 + 生成面板 + 推 Lark", 1
        )[0]

        self.assertIn("path: data/state.json", core)
        self.assertIn("ca-dedup-state-", core)
        self.assertNotIn("reference_prices.json", core)
        self.assertNotIn("cik_map.json", core)
        self.assertIn("reference_prices.json", support)
        self.assertIn("cik_map.json", support)
        self.assertNotIn("state.json", support)
        self.assertIn("actions/cache/save@v4", text)

    def test_failed_build_cannot_publish_pages_and_state_is_validated(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("always()", text)
        self.assertIn("python tools/validate_state.py", text)
        self.assertIn("python tools/validate_public_snapshot.py", text)
        self.assertIn("needs.build.result == 'success'", text)


if __name__ == "__main__":
    unittest.main()
