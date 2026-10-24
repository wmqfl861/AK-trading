# 当前策略优先规划

生效规划版本：`revisions/r2-001`；记录日期：2026-10-15。

- [蓝图](revisions/r2-001/BLUEPRINT.md)：策略与真实研究优先；MINUTE/DAILY 各自 ready 即交回测报告；纯模拟启动、影子完整验收、真实订单许可分别处理。
- [实施计划](revisions/r2-001/IMPLEMENTATION-PLAN.md)：数据、必要代码/性能、回测报告、模拟消费准备按不相干写集并行；具体模块计划、lease、单一 writer 与原集成顺序继续适用。
- [修复顺序](revisions/r2-001/REPAIR-ORDER.md)：只让实际消费者所需的 M1/P2/DAILY/R5 等缺口进入关键路径；不把通用平台变成共同前置。
- [原作者处置](revisions/r2-001/DISPOSITION.md)：两项实质修正与三项非阻断观察的处置。
- [独立 ZCode 最终窄复审](zcode-plan-review-002/OWNER-REPORT.md)：`VERDICT: PASS`，`requires_plan_update=false`；原调用原件在同目录 stdout.json、stderr.txt、TERMINAL.json、RESULT.json。

根目录 BLUEPRINT.md / IMPLEMENTATION-PLAN.md / REPAIR-ORDER.md 为已审 r1 历史版本，只读保留；当前执行规划引用以上 r2-001，避免沿用 r1 中双频等待和模拟门槛混用的表述。

审核证据：r1 完整计划审核 PASS；r2 对两项实质修正、三项处置及合同保留的必要窄复审 PASS。最终 CLI/watchdog native exit 均为 0；监督器启动前和终态后核对所有固定输入，`frozen_inputs_unchanged=true`。两次计划审核的审核者独立 hash 执行为 NOT_RUN（plan 模式），不能写成审核者独立执行了 hash/测试。最终审核者报告的一次可选 docs Glob 超时保留，报告明确必需文件已读齐，没有阻断项，不进行重复整轮审核。

2026-10-16，按当前有效 Goal 和用户最新纠正执行：Astra/max 负责必要计划，实际 ZCode GLM-5.3/max 负责开发与修复，另一独立 ZCode GLM-5.3/max 负责审核；Sol/max 仅作为其他规则未指定模型的辅助子代理默认值。具体角色以 [项目模型与职责规则](../../PROJECT-MODEL-ROLES.md) 为准。策略端优先，互不干扰的模块并行。冻结规划及审核原件中的 paused 是发布时历史状态，原件不改写；此前已续接的任务和实际作者身份如实保留，后续开发按纠正后的角色显式接续。已有效计划满足本模块前置后立即实施，冻结后立即硬审；不为相同偏好重复规划或确认。此状态记录本身不新增产品硬审、回测、模拟或策略资格信用。
