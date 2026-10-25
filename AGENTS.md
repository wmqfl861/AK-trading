# AK-Trading Project Rules

<!-- Authoritative project rules. Established by direct user instructions on 2026-09-14.
     These rules govern all agents working in this repository. Where any older text
     conflicts with this file, this file prevails. Global Iron Law (user-level AGENTS.md)
     remains fully in force and is not restated here. -->

## 1. Roles: Astra Plans, ZCode Builds, Independent ZCode Reviews

Current role authority: `PROJECT-MODEL-ROLES.md`, updated by the user's explicit
instruction on 2026-10-25 to resume parallel development and use ZCode for development.
Actual development and product repairs are assigned to ZCode CLI sessions;
this supersedes older Sol and Claude Code developer wording in plans, work orders and session history
for every new dispatch or continuation. Role-specific assignments take precedence over the default
model for otherwise-unspecified auxiliary subagents.

- **Codex (gpt-6-astra / reasoning `max`) is the planning authority.** Codex writes: development
  plans and dev-plan documents (per-node, detailed), specifications, change requests, contract
  arbitrations/rulings, and review briefs. Codex must be invoked via the Iron-Law watchdog
  wrapper with `-m gpt-6-astra -c model_reasoning_effort=max`.
- **All development must be preceded by a Codex-authored plan.** No node, module, or feature is
  implemented without a corresponding written plan from Codex (docs/devplan-v2/*, a dispatched
  Codex plan task, or an arbitration decision). Ad-hoc implementation without a plan is
  prohibited. When a gap is discovered mid-development, it is escalated to Codex
  (arbitration/clarification), not resolved silently by the implementer.
- **Plan review provides advice, not hard-review acceptance.** As explicitly corrected by
  the user on 2026-10-18, independent ZCode supplies findings, risks and recommendations.
  The original Astra plan author alone decides whether and how to revise the plan, records
  the disposition and publishes the effective version; a no-change decision keeps the
  existing plan effective. No plan-review PASS is required. A reviewer's PASS/FAIL label or
  request for an update is advisory and cannot itself become a development gate. Root must
  not require repeated plan reviews to obtain PASS. The author's decisions must respect
  the user's authorized scope and existing hard acceptance requirements.
- **Actual development and product repairs use ZCode CLI.**
  Invoke the installed `zcode-cli` skill using ZCode's existing model and reasoning
  configuration; this routing change does not authorize changing that configuration.
  Assign a dedicated owner session independent
  from the plan author and product reviewer, with the effective plan, dependencies,
  exact file ownership, findings, acceptance criteria and evidence paths supplied in
  the work order. Do not rely on inherited conversation context.
  Continue the same ZCode developer for fixes with its exact `--resume` session ID,
  original cwd and fixed session store; record a necessary ownership transfer explicitly.
  Existing Sol or Claude Code developers must stop writing before their effective
  plans, current revisions, unresolved findings and verification state transfer
  explicitly to ZCode. Preserve historical authorship,
  old failures and consumed one-use grants; do not overlap writers or restart a
  consumed command. Sol and Claude Code must not be dispatched or resumed for
  further development under their old work orders. Auxiliary agents cannot replace
  the designated ZCode developer or be labeled as that developer. This is task-scoped
  routing authorization, not permission to edit persistent provider, credential,
  model, context or compression configuration. No silent model fallback is allowed.
- **Independent ZCode supplies plan feedback and performs product hard reviews.** Use
  independent ZCode `GLM-5.3` / `max` sessions that took no part in the reviewed node's
  development or repair. Codex and developer self-checks cannot issue the independent
  product PASS. Astra's disposition of plan feedback is the plan author's own decision,
  not a product hard review.
- **Reviews are executed against Codex's plan.** The review standard is the Codex-authored
  documents (dev-plan 00–07, change requests 001–006, review briefs, arbitration decisions).
  A reviewer must not invent new acceptance criteria or relax existing ones.
- Product hard-review protocol: independent re-runs of required commands, reviewer-authored counterexample
  probes, per-finding evidence with minimal reproduction, verdict PASS/FAIL with no conditional
  pass. FAIL → original owner repairs → fresh independent re-review against new frozen hashes,
  until PASS. Audit packages are read-only to owners and reviewers after verdict.
- **Default for otherwise-unspecified subagents.** When a user instruction, project rule,
  plan, work order, or agent-specific rule does not specify a different model or agent family,
  dispatch the auxiliary task through OpenCode using its own configured default model and
  default reasoning settings. Do not add a model, provider, variant, or reasoning-effort
  selector solely because this fallback applies. Explicit role or model assignments take
  precedence; this fallback does not override the planning, implementation, or independent-
  review assignments above. External OpenCode dispatch remains subject to the watchdog-wrapper
  and evidence rules in §5.

## 2. Parallel Development

- **Maximize parallelism as a standing mandate.** Continuously scan the development blueprint
  (Codex plans) for any node, module, or work item whose dependencies allow concurrent
  development, and run as many subagents in parallel as useful. When an item is gated behind
  another's output (e.g. frozen contracts), dispatch its owner in *prepare mode* ahead of the
  gate (read plan, map H-clauses, design notes, test skeletons, dependency lists) so it flips
  to full-speed implementation the moment the gate opens. Idle windows between dependency
  stages are waste; avoid them.
- Develop nodes and modules in parallel wherever dependencies allow; dispatch as many
  subagents as useful. Cross-node contracts are resolved by Codex arbitration, never by
  one node silently editing another node's files.
- Each node owns an explicit file set. Frozen-hash discipline: every submission/evidence
  package records per-file SHA-256; prior revisions are preserved, never overwritten.
- Coordination discipline: cross-node regressions are routed to the owning node with the
  reproducing evidence; owners do not "fix" other nodes' files or mask failing tests. A
  plan's stated integration order (e.g. C1→C2→C3 stages) is binding; parallelism must not
  skip gates, but preparation before a gate is encouraged.
- **While a review is in progress, its frozen file set is write-frozen for owners too.**
  Post-freeze work that would touch files under active review (e.g. a newly unlocked
  follow-up work order) must be queued until the verdict lands, or the review scope must
  be explicitly re-cut first. Dispatching file-modifying work into an active review's
  file set invalidates the review (NE1 C2 r1 F01 lesson, 2026-09-15).

## 3. Evidence and Honesty

- No fabricated evidence: no fake statistics, no epsilon/jitter patches to pass checks, no
  backdated provenance, no undisclosed skips. Limitations (INSUFFICIENT / NOT_RUN /
  PARTIAL_UNVERIFIED / SCENARIO-only) are reported as-is.
- Synthetic inputs are labeled SYNTHETIC and never presented as historical PIT data.
  Historical calendar gaps are recorded as gaps; SCENARIO reconstruction is named as such.
- Usage-limit (1308) interruptions: work resumes from each agent's standing work order;
  completion claims require re-run evidence, not memory.

## 4. Trading Safety Boundary

- Real order submission/cancellation requires ALL of: execution-domain hard review PASS,
  deployment + simulation acceptance with real send/cancel counts = 0, and the user's explicit
  authorization with trading enabled in the client UI. Default is `enable_trading=False`
  everywhere; observer mode always answers `hold`.
- Real broker interaction remains `broker=NOT_RUN` until the native execution adapter node
  passes its own plan→develop→review cycle and the user authorizes the first real order.
- Credentials (.env, TQ_USER/TQ_PASS, DB passwords) are never printed or read by agents.

## 5. Infrastructure

- External CLI dispatch (codex/claude/opencode/zcode) only through the watchdog wrapper with unique
  task_id + dedicated log path (Iron Law Art. 2–3). ZCode-native Agent-tool subagents need no
  wrapper. ZCode CLI uses the established session-store-aware watchdog: each owner
  and independent reviewer has its own fixed store, with at most one active call per store.
  Different verified stores may run in parallel; preserve session identity on continuation.
- Before every dispatch or continuation, classify the actual task and read the current
  role/workflow authority. For development, verify the ZCode owner/session, existing
  configuration and exact file ownership. Old work-order pins, owner history or convenience
  cannot restore a superseded Sol/Claude Code development route.
- Web search only via the local stack (Iron Law Art. 4). No agent may modify any model/LLM
  configuration (Iron Law Art. 1).
- Running services: the observer bridge server (127.0.0.1:17778) is project infrastructure;
  restarts are a main-session action recorded in maintenance artifacts. The InfiniTrader client
  UI (loading/reloading strategy instances) is operated by the user only.
- Client deployment target: `...\pyStrategy\self_strategy\AKObserverBridge.py` (the
  self-contained dual-class file; keep `AIBridge.py` in the same directory byte-identical).
  Always back up the previously deployed bytes before replacing.

## 6. Current Workflow State Pointers (2026-09-14)

- Contract lock: `data/research_runs/v2/contracts-2.1.2/contract-lock.json`
  (SHA-256 `ac211ec9…e93b66`, devplan-v2.1.5). All formal runs bind to it.
- Hard-review status: M1 r4 PASS (lock published); execution domain r3 PASS; N1a v5 PASS;
  M4 r3 review in progress; M2/M3A/M3B formal reviews per audit backlog.
- Observer: v5 (`observer-20260914-reconnect-v5`) deployed; night-session acceptance window
  pending. Execution: simulation acceptance r1 recorded with real sends = 0; the client native
  execution adapter (real orders) is the next planned node and requires a Codex plan first.

## 7. Native Command Exit Codes and Optional Discovery

Established after the 2026-10-04 optional-search incident; applies to Root and every
dispatched agent. See `docs/maintenance/20261004-rg-no-match-r1/INCIDENT.md`.

- Before running a discovery command, decide whether an empty result is allowed.
  `rg` uses exit 0 for matches, 1 for no matches, and 2 for an error. For optional
  discovery only, record exit 1 with empty stderr as `NO_MATCH` and continue bounded
  discovery. An empty result never satisfies a required-file or acceptance condition.
- Capture the native exit code immediately, together with stdout and stderr. Prefer
  an argument-array subprocess when shell exit propagation is ambiguous. For `rg`,
  return codes other than 0/1, unexpected stderr, launch failures and timeouts remain
  errors. Never use a blanket success exit, `|| true`, or discarded stderr to hide them.
- Do not leave an optional `rg` as the final unclassified command in a batch. Batch
  independent reads at the tool-orchestration level, retaining each result separately;
  a later successful command must not erase an earlier failure.
- These meanings are specific to `rg`. Tests, builds, migrations, agent CLIs and
  watchdogs keep their own exit contracts; exit 1 from those commands is not NO_MATCH.
- In PowerShell, materialize diagnostics as text before an explicit exit and ensure
  they are actually present in the tool result. An empty tool result, even with exit 0,
  is not evidence that the expected diagnostic or validation ran successfully.
- Include this exit-code rule and the incident reference in new agent work orders.
  When reporting failures, identify the exact command, raw exit code and semantic
  outcome; preserve original failure evidence instead of retroactively changing it.

## 8. CLI Diagnostic Preconditions and Grok Approval Recovery

Established after the second diagnostic `Failed (exit 1)` incident on 2026-10-04.
Read `docs/maintenance/20261004-grok-headless-cancel-r1/INCIDENT.md` before changing
agent CLI invocation or diagnosing Grok cancellation.

- Check existence before enumerating optional documentation directories. Record
  `NOT_PRESENT_OPTIONAL`; missing required inputs remain errors.
- Inspect this machine's actual CLI response schema before writing an adapter.
  In this Grok build, ACP `session/new` has no `modes.currentModeId`; validate
  actual model/effort fields and the runtime permission evidence instead of assuming
  a field exists. Never change model configuration to make a probe pass.
- After a PowerShell native invocation, capture `$LASTEXITCODE` immediately and
  explicitly pass it through after materializing diagnostics. Implicit PowerShell
  completion can collapse native 3 or 7 into 1. Expected-negative probes run inside
  a capturing subprocess harness and must assert their precise expected outcome;
  their raw failures are preserved, never relabeled as successful commands.
- Grok native 0 and `end_turn` do not establish task success. Inspect tool failures,
  cancellation, actual artifacts and verdicts separately. `User cancelled the
  execution` alone does not prove a human cancellation.
- Auto classifier `Unavailable` and explicit Auto `Block` are different outcomes.
  The former may fall back to confirmation; unattended execution needs a real
  approval receiver. Only existing user-authorized, exact, hash-bound, single-use
  commands may be granted. Explicit rejection remains stopped; do not switch to
  bypass permissions, retry, or transform the command to evade it.
- Preserve original process/session identity and old evidence. Resume with the
  byte-exact cwd from the original REQUEST: on this Windows build, changing its
  slash spelling can make ACP session/load report FS_NOT_FOUND. Preserve replayed
  history separately and do not count historical tool failures as current-prompt
  failures. Never replace an owner/reviewer session that must be resumed.
- The user explicitly authorized session-scoped exact confirmation for this
  project's already authorized Grok reviews on 2026-10-04. The authorization is
  pinned in `docs/maintenance/20261004-grok-headless-cancel-r1/manifest-recovery-001/USER-AUTHORIZATION.json`.
  Do not ask again for that same permission. Keep the authorized model/effort,
  yolo=false, exact commands, unchanged input pins and single-use grants. Follow
  the current maintenance `USAGE.md`; missing/mismatched authorization must stop
  before launch. The real v4 recovery exercised one exact confirmation successfully.
- A generated output is not an input. Before invoking Read on a path, determine
  whether it is a required existing input or a not-yet-created output. Persist
  generated-output preconditions before launch and pass them explicitly to the
  work order; do not use a deliberately failing Read as an existence check.
  A missing required input, undeclared path, cancelled tool, failed writer or
  invalid final artifact remains a failure. Never blanket-ignore FileNotFound.
- Native CLI exit, receiver exit, individual tool result, artifact completion
  and product verdict are separate facts. An unresolved tool failure is not
  permission to rerun a successful one-time command. Inspect actual outputs and
  append a specific evidence-bound disposition where justified, retaining the
  original RESULT and failed tool. See the manifest-recovery-002 exit 3 incident
  and detached `manifest-custody-001/RECEIPT.json`; its product verdict remains FAIL.
  New work orders must carry these rules and both incident references.
- For future single-write FAIL-manifest tasks, use the independently accepted
  `exit3-implementation-r1/manifest_output_guard.py` preflight/complete companion
  as documented in the maintenance `USAGE.md`. Preflight is neither authorization
  nor task completion. Prospective completion requires zero failed current tools;
  its historical disposition is restricted to the already sealed exact event.
- Obtain cwd, commands and expected hashes programmatically from their pinned
  authoritative records. Do not retype long hash/path literals in diagnostic
  probes. Check required cwd and source pins before launching a replay; retain
  reviewer-harness errors separately from owner findings. The independent review's
  probe-a-attempt1 typo incident is preserved in its FINDINGS.json.
- Custody and capture wrappers must normalize every path-like input at the boundary
  with `Path(value)` before calling filesystem methods such as `.resolve()` or
  `.relative_to()`, and must reject unsupported types explicitly. A child command
  that exits 0 does not make a post-termination pinning failure successful; preserve
  the wrapper's raw nonzero exit and record the child, custody, artifact, and product
  verdicts separately. The R1 regression is recorded in
  `docs/maintenance/20261016-r1-custody-path-normalization-r1/INCIDENT.md`.
- PowerShell capture wrappers must not assign to automatic or reserved variables such
  as `$PID`; use an explicit name such as `$childPid`, capture it immediately after
  launch, and record the child identity before waiting or sealing streams. A wrapper
  that exits before child/result capture is `SUPERVISOR_NOT_CAPTURED`; preserve that
  attempt and use a distinct successor command when the operation is safely replayable.
- JSON custody and precondition writers must serialize and validate their complete
  payload before creating the final path, or write a temporary sibling and atomically
  replace it after success. A zero-byte or truncated final file is a failed writer
  artifact; preserve it and use a new attempt name instead of overwriting it.
  In Python, pass a real newline value (`newline="\n"`) or omit the argument; a
  literal backslash sequence such as `"\\n"` is invalid for `pathlib` and can leave
  a zero-byte file after native exit 1.
- Freeze source before review, but seal stdout/stderr and run evidence only after
  their producing processes have terminated. A manifest writer must exclude its
  own open output streams and the still-running parent CLI's output. Pin those
  later in a detached supervisor custody manifest. An early zero-byte log pin
  that changes at normal completion is still a real integrity mismatch: preserve
  it, bind an explicit terminal disposition, and publish a new receipt instead
  of overwriting the old manifest or suppressing the failure.

## 9. Advisory Plan Feedback, Immediate Development and Product Hard Review

Established by direct user instruction on 2026-10-09. The detailed companion rule is
`PROJECT-WORKFLOW-RULES.md` in this repository. The user's explicit correction on
2026-10-18 removes any mandatory plan-review PASS gate and takes precedence over older
conflicting rules and work-order wording for subsequent dispatches.

- Independent ZCode reviews a plan to provide opinions only. The original Astra plan
  author decides ADOPTED, PARTIALLY_ADOPTED or NOT_ADOPTED with reasons, then publishes an
  updated effective plan or confirms the existing plan without changes. Whether to modify
  the plan belongs to Astra, not the reviewer or Root; plan review need not PASS.
- Once that effective plan and its required preconditions are ready, Root immediately
  assigns a ZCode implementation session, independent from the plan
  author. Do not wait for plan-review PASS, add a second planning round, repeat review
  merely to obtain a favorable label, or wait for another Root or user confirmation.
- After implementation, required verification, and file/hash freeze finish, Root
  immediately assigns an independent ZCode hard review of the actual write-set. Plan
  review, developer self-tests, and evidence custody do not count as product hard
  review.
- A genuine hard dependency, active review write-freeze, or missing required
  authorization may stop that module. Non-blocking observations and optional discovery
  diagnostics must be recorded without becoming an extra approval gate; unrelated ready
  modules continue in parallel. A plan-review label or an unadopted recommendation is
  not itself a hard dependency.
- A hard-review FAIL returns to the same actual ZCode implementation owner for an append-only repair;
  the repaired frozen revision is immediately re-reviewed by independent ZCode.

## 10. TqSdk、凭据与默认交易模式

- 任何 TqSdk／天勤／国内期货量化任务都必须先读取 `tqsdk-trading-and-data` skill 的
  `SKILL.md`，再只读取所需的 `references/*`；不得臆造 TqSdk API。
- TqSdk skill 路径：项目 `.skills/tqsdk-trading-and-data/tqsdk-trading-and-data/`（若该路径不存在，使用已安装的共享、Codex、Claude 或 OpenCode skill 路径）。
- 凭据只能从 `.env`、`TQ_USER` 和 `TQ_PASS` 等环境变量读取；不得打印、硬编码或提交真实凭据。
- 默认使用本地 `TqSim`；只有用户明确要求并完成项目规定的安全授权时，才使用 `TqKq` 或真实 `TqAccount`。
