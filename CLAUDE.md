# AK-Trading: current human-authorized project rules

Before working, read `AGENTS.md`, `PROJECT-MODEL-ROLES.md`,
`PROJECT-WORKFLOW-RULES.md` and `PROJECT-MEMORY.md` in this repository.

The human explicitly instructed on 2026-10-18:
> 修改项目规则，开发agent改为gpt-6.1-sol/max子代理。并请确保用新的规则

Actual development and product repairs use Codex's own `gpt-6.1-sol` / `max`
subagents, created with explicit `model=gpt-6.1-sol`,
`reasoning_effort=max`, and `fork_turns=none`. Claude Code
must not continue development under an older work order: stop writing and transfer
the existing plan, WIP, findings, verification and consumed-command state explicitly
to the Sol owner. Historical authorship remains unchanged. This latest human
instruction overrides older Claude Code/ZCode developer wording. Do not alter
persistent LLM configuration or substitute a different model. Astra authors effective plans; independent ZCode GLM-5.3/max
provides advisory plan feedback and performs product hard reviews. Unspecified
auxiliary delegation uses OpenCode defaults. Follow the assigned file ownership,
existing plans, verification requirements and trading boundaries in the root rules.
No new development plans: complete this existing batch through independent hard
review and stop. Historical work orders remain evidence, not current role authority.
