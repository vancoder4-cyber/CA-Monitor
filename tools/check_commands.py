# -*- coding: utf-8 -*-
"""指令一致性检查:确保 CA问答助手的指令在六处保持同步——
  ① 唯一来源 bot/cards.py 的 COMMANDS
  ② bot/bot.py on_message 的 dispatch 分支
  ③ HELP_TEXT(自动生成,顺带校验)
  ④ README.md 的「指令清单」
  ⑤ bot/README.md 的部署/使用说明
  ⑥ OPERATIONS_MANUAL.md 的全量操作手册

用法:  python tools/check_commands.py   (一致 → 退出码 0;不一致 → 打印差异并退出码 1)
建议每次改指令后都跑一遍(CI 里也会自动跑)。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))
import cards  # noqa: E402

keys = [c["key"] for c in cards.COMMANDS]
errors = []

# ① vs ② —— bot.py dispatch 覆盖
bot_src = open(os.path.join(ROOT, "bot", "bot.py"), encoding="utf-8").read()
dispatched = set(re.findall(r'cmd == "([a-zA-Z_]+)"', bot_src))
for k in keys:
    if k not in dispatched:
        errors.append(f"[dispatch] COMMANDS 的 '{k}' 在 bot.py 里缺少 `cmd == \"{k}\"` 分支")
for k in sorted(dispatched):
    if k not in keys:
        errors.append(f"[dispatch] bot.py 有 `cmd == \"{k}\"` 但 COMMANDS 里没有此指令")

# ① vs ③ —— HELP_TEXT 覆盖
for c in cards.COMMANDS:
    if c["name"] not in cards.HELP_TEXT:
        errors.append(f"[help] '{c['name']}' 未出现在 HELP_TEXT")

# ① vs ④ —— README 指令清单覆盖
readme_path = os.path.join(ROOT, "README.md")
readme = open(readme_path, encoding="utf-8").read() if os.path.exists(readme_path) else ""
for c in cards.COMMANDS:
    if not (c["name"] in readme or c["key"] in readme or c["kw"][0] in readme):
        errors.append(f"[readme] 指令 '{c['name']}'({c['key']}) 未写进 README.md")

# ① vs ⑤ —— bot README 不能继续保留旧指令表
bot_readme_path = os.path.join(ROOT, "bot", "README.md")
bot_readme = open(bot_readme_path, encoding="utf-8").read() if os.path.exists(bot_readme_path) else ""
for c in cards.COMMANDS:
    if not (c["name"] in bot_readme or c["key"] in bot_readme or c["kw"][0] in bot_readme):
        errors.append(f"[bot-readme] 指令 '{c['name']}'({c['key']}) 未写进 bot/README.md")

# ① vs ⑥ —— 全量操作手册第 9 节的指令表必须精确覆盖所有指令。
# 不做全文子串搜索，否则删掉表格行后，正文里的同名词仍会让检查假通过。
manual_path = os.path.join(ROOT, "OPERATIONS_MANUAL.md")
manual = open(manual_path, encoding="utf-8").read() if os.path.exists(manual_path) else ""
if not manual:
    errors.append("[manual] 缺少 OPERATIONS_MANUAL.md")
else:
    match = re.search(r"^## 9\..*?(?=^## 10\.)", manual, re.M | re.S)
    if not match:
        errors.append("[manual] OPERATIONS_MANUAL.md 缺少第 9 节完整指令表")
    else:
        manual_names = [
            re.sub(r"[`*_]", "", name).strip()
            for name in re.findall(r"^\|\s*\d+\s*\|\s*([^|]+?)\s*\|", match.group(0), re.M)
        ]
        expected_names = [c["name"] for c in cards.COMMANDS]
        missing = [name for name in expected_names if name not in manual_names]
        extra = [name for name in manual_names if name not in expected_names]
        if missing:
            errors.append("[manual] 第 9 节缺少指令：" + ", ".join(missing))
        if extra:
            errors.append("[manual] 第 9 节存在未声明指令：" + ", ".join(extra))
        if len(manual_names) != len(expected_names):
            errors.append(
                f"[manual] 第 9 节指令行数为 {len(manual_names)}，应为 {len(expected_names)}"
            )

# ⑦ CHANGELOG.md 存在且可解析(至少一条 `## ` 条目)
changelog_path = os.path.join(ROOT, "CHANGELOG.md")
if not os.path.exists(changelog_path):
    errors.append("[changelog] 缺少 CHANGELOG.md(每次修复完成时必须立即记录)")
else:
    cl = open(changelog_path, encoding="utf-8").read()
    if not re.search(r"^## ", cl, re.M):
        errors.append("[changelog] CHANGELOG.md 没有任何 `## 日期 · 标题` 条目")

if errors:
    print("❌ 指令一致性检查未通过:")
    for e in errors:
        print("   -", e)
    sys.exit(1)

print(f"✅ 指令一致性检查通过:{len(keys)} 个指令在 COMMANDS / bot.py / HELP_TEXT / README / bot README / 操作手册六处一致。")
print("   指令:", ", ".join(keys))
