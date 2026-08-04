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


if __name__ == "__main__":
    unittest.main()
