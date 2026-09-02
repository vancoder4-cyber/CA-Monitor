# 仓库维护规则

## Bot「最近更新」同步（强制）

任何 bug 修复、用户可见行为变更、规则或配置修订、数据修正、文案调整，都必须在**同一项任务完成时**同步更新根目录 `CHANGELOG.md` 的最上方；没有更新，不得宣告任务完成。

- 不得等到 push、合并或部署时再补。只读查询且仓库没有发生变化时可免。
- 同日同一主题可追加到顶部现有条目；不同主题新增 `## YYYY-MM-DD · 标题`，日期必须是真实当天日期。
- 最终交付前必须检查 `git diff -- CHANGELOG.md`，确认本次修复已被明确记录。
- 必须区分「本地已修复」和「生产已更新」：Bot 不直接读取 Markdown，而是读取 Pages 的 `data.json`；链路为 `CHANGELOG.md` → `run.py build` → Pages `data.json` → Bot「最近更新」。
- 若本次任务包含发布，只有 Actions build / Pages deploy 完成，并确认生产 `data.json` 的 `changelog[0]` 是本次条目后，才可宣告 Bot「最近更新」已刷新。
- `site_data.json` 是构建产物，不得手工修改来伪造更新；应由正常构建生成。
