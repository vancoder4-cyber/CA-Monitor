import tempfile
import json
import unittest
from pathlib import Path

import report
import run
from bot import cards


class ChangelogRenderingTests(unittest.TestCase):
    def test_build_and_bot_share_complete_changelog_contract(self):
        self.assertEqual(report.load_changelog(), run.load_changelog())
        entry = {"head": "fixture", "items": [f"item-{n}" for n in range(1, 9)]}
        rendered = json.dumps(cards.changelog_card({"changelog": [entry]}, ""), ensure_ascii=False)
        self.assertIn("item-7", rendered)
        self.assertIn("item-8", rendered)

    def test_long_nested_changelog_is_complete_and_safe(self):
        markdown = """# 更新日志

## 2026-09-03 · 测试版本
- **第一项**
- 第二项
- 第三项
- 第四项
- 第五项
- 第六项
- 第七项
- 第八项
  - 子项含 `state.json`
    - 深层链接 [官方说明](https://example.com/docs?a=1&b=2)
- <script>alert(1)</script> [危险链接](javascript:alert(1))
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "CHANGELOG.md"
            path.write_text(markdown, encoding="utf-8")
            entries = report.load_changelog(str(path))

        self.assertEqual(1, len(entries))
        self.assertEqual(11, len(entries[0]["items"]))
        self.assertEqual("子项含 `state.json`", entries[0]["tree"][7]["children"][0]["text"])
        self.assertEqual(
            "深层链接 [官方说明](https://example.com/docs?a=1&b=2)",
            entries[0]["tree"][7]["children"][0]["children"][0]["text"],
        )

        rendered = report._changelog_entries_html(entries)
        self.assertIn("<strong>第一项</strong>", rendered)
        self.assertIn("第七项", rendered)
        self.assertIn("第八项", rendered)
        self.assertIn("子项含 <code>state.json</code><ul><li>深层链接", rendered)
        self.assertIn(
            '<a href="https://example.com/docs?a=1&amp;b=2" target="_blank" '
            'rel="noopener noreferrer">官方说明</a>',
            rendered,
        )
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)
        self.assertNotIn('href="javascript:', rendered)

    def test_inline_renderer_escapes_html_before_enabling_allowed_markup(self):
        rendered = report._inline_markdown_html(
            '**<img src=x onerror=alert(1)>** `</code><script>x</script>` '
            '[安全](https://example.com/\" onclick=\"alert(1))'
        )

        self.assertNotIn("<img", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("<a href=", rendered)
        self.assertIn("<strong>&lt;img", rendered)
        self.assertIn("<code>&lt;/code&gt;&lt;script&gt;x&lt;/script&gt;</code>", rendered)


if __name__ == "__main__":
    unittest.main()
