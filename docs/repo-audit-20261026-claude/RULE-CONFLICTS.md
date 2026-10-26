# A3：规则入口差异提议（已获用户确认）

用户于本轮明确回复“同意仅同步这两处（推荐）”。Root 已按下面逐字提议同步 CLAUDE.md；仅修正两处过期内容，不采纳外部蓝图 F1–F6。当前实际派工遵循 `AGENTS.md`、`PROJECT-MODEL-ROLES.md` 和 `PROJECT-WORKFLOW-RULES.md` 的 2026-10-25 ZCode 开发职责。

| 位置 | 当前文字与问题 | 拟议处理 |
| --- | --- | --- |
| `CLAUDE.md:6–15` | 仍引用 10-18 的 Sol 开发指令、要求移交给 Sol，并把 ZCode 开发称为旧路由；与当前三份规则的 ZCode 开发职责冲突 | 只同步为已生效的 10-25 ZCode 开发路由；保留 Astra 计划、独立 ZCode 审核、OpenCode 默认辅助与配置保护 |
| `CLAUDE.md:20–21` | 旧批次的 “No new development plans” 未标适用批次；容易误挡用户本次明确授权的第二轮修复增补 | 表述为遵守当前用户授权批次及有效计划，不自动扩大下一批范围 |

建议将 `CLAUDE.md:6–21` 替换为以下文字，其他内容保持原样：

```text
The human explicitly instructed on 2026-10-25 that actual development and
product repairs use dedicated ZCode CLI sessions with their existing model
and reasoning configuration. This supersedes older Sol/Claude Code developer
routes for new dispatches and continuations. Preserve historical authorship;
verify the previous writer has stopped before an explicit ownership transfer.
Do not alter persistent LLM configuration or silently substitute another model.
Astra authors effective plans and disposes of advisory plan feedback.
A separate independent ZCode GLM-5.3/max session supplies plan advice and
performs product hard reviews; it must not develop or repair the same product.
Unspecified auxiliary delegation uses OpenCode defaults. Follow file ownership,
freeze requirements, current role/workflow authority and trading boundaries.
Work within the current human-authorized batch and effective plans/addenda.
Complete authorized deliverables and independent hard review, then stop without
automatically starting a new batch. Historical work orders remain evidence,
not current role authority.
```

确认结果：仅按以上两处同步 `CLAUDE.md`，已执行。README 测试状态与 r3 实施计划的角色描述继续按本批授权更新。AGENTS.md、PROJECT-MODEL-ROLES.md、PROJECT-WORKFLOW-RULES.md 没有修改。
