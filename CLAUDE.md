# AK-Trading: current human-authorized project rules

## 2026-11-07 本批有效规则：G1–G4

用户直接批准以下四项；本批内覆盖旧规则、计划增补和工单中与之冲突的并行、运行消费、预算及状态记录要求，其他规则不变。

- **G1 并行上限**：关键路径同时最多一条开发线和一条独立审核线，另留一条支撑线做 CI 和小修；第一份 DAILY 报告之前不开新模块。
- **G2 工程运行可重跑**：工程测试、工具和监督器运行可以重跑。因配额、5xx、磁盘、路径或监督器超时而失败的运行不算消费；只有最终 holdout 评估保留一次性。保留真实失败记录，不复用旧的已消费机会或过期窗口；本批工程重跑使用新的运行标识。
- **G3 简化预算**：取消逐次调用的工具秒预算和累计记账上限。每个作业只设一个宽松的墙钟超时，启动前检查一次资源，不再因工具秒数或累计账额开新的处置循环。
- **G4 交付与记录**：状态只维护 H1 的一页 `docs/CURRENT-STATUS.md`，每周覆盖；旧 Goal STATUS 冻结。原始日志压缩后放仓库外，仓库只留摘要。以“合入 main 且 CI 通过”为完成标准；既有独立硬审核要求保持。每项完成即提交并推送 main，提交说明记录命令、原始退出码和 passed/failed 数，不为每一步新增过程文档。

本次只采纳 G1–G4；G5、G6、分支保护、全库换行规范化和项目 Python 支持版本待用户另行决定。


Before working, read `AGENTS.md`, `PROJECT-MODEL-ROLES.md`,
`PROJECT-WORKFLOW-RULES.md` and `PROJECT-MEMORY.md` in this repository.

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
