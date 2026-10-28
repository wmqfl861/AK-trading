# r005 — response-only native-entry adapter: actual-source test seam

Revision r005 (task `m5-native-entry-adapter-dev-020-001`, same owner, author020).
Task-local modified invocation code; the installed bundle is read-verified and
never modified. Transforms `T1b`/`T1c`/`T2`, admission and mandatory-exclusive
capture semantics are unchanged from r004. What changed:

1. **Shared transform implementation.** `apply-transforms.cjs` now holds the
   deterministic pin-verify + counted-transform operation, imported by BOTH the
   production preload (`response-only-entry.cjs`) and the test seam — author020
   §2 requires the same implementation with no second copy.
2. **Actual-source test seam** (`test-actual-source.cjs`) replaces the retired
   r004 whole-CLI route. No CLI bootstrap/main/profile-loader/auth-init/plugin
   startup/provider factory is executed. Instead, the pinned bundle is verified
   and transformed with the shared helper, and the relevant NATIVE units are
   CHECK-EXTRACTED from the transformed bytes (balanced quote-aware boundaries;
   missing/ambiguous matches exit closed) and executed in `vm` sandboxes whose
   free variables resolve only through a recording proxy (explicit inert
   dependencies + opaque identity markers; whitelisted JS intrinsics only):
   - `U-siteA`/`U-siteB` — the two actual headless construction statements;
   - `U-soe` — actual built-in registration loop; `U-ffa` — actual built-in
     registry-entry transformer; `U-ibn` — actual deferred-MCP registration;
   - `U-nyo` + `U-mma` — the actual dispatcher entry and executor, including
     the stock `ToolNotFound` / `recoverable:false` rejection branch.
   Extraction provenance (anchors, char ranges, per-slice SHA-256) is written
   exclusively BEFORE any unit executes (`EXTRACTION-r005-<runid>.json`).

## What the executed checks established (RUNID=005, 21/21 PASS, raw exit 0)

- **P1–P3**: the actual transformed constructions execute; both bind
  `runtimeConfig.toolAllowlist = []` and preserve the opaque authentication-port
  marker, session id, cwd/working directory, env, provider registry and model
  selection markers by identity.
- **P4–P5**: with NONEMPTY candidate populations (5 synthetic built-ins, 2
  synthetic deferred-MCP tools), the actual `Soe`/`Ibn` registration leaves the
  registered registry empty (0 registered, 0 admitted).
- **P6**: the real `guard-core.cjs` returns verdict OK (toolCount 0 AND
  registeredCount 0) on a session derived from the construction result.
- **P7**: an absent-tool call reaches the ACTUAL rejection branch — code from
  `fr.ToolNotFound`, message text `Tool not found: Zro117SyntheticUnavailableTool`
  built by the actual template, `recoverable:false`, one persisted-failure call,
  and `getMode` (executor path) never invoked — no handler execution.
- **P8**: zero external references outside the declared inert dependency set.
- **N1–N3**: without the binding the guard fails `ALLOWLIST_NOT_BOUND`; a
  registered tool hidden behind an empty filtered list fails
  `REGISTERED_REGISTRY_NOT_EMPTY`; without the empty allowlist the same
  nonempty populations DO register (5/5 and 2/2) and are filtered only by the
  binding (0 with `[]`). The RUNID=004 observation that `Glob`/`Grep` are
  excluded by the actual embedded-search branch rule (independent of the
  allowlist) is preserved in that run's records.
- **K22/K19/K20/K18/K18b**: mandatory exclusive capture re-verified once
  against the factored r005 preload (fresh-file claim; absent → exit 7;
  occupied → exit 7 with EEXIST; directory → exit 7; missing parent → exit 7
  with ENOENT).

Checkpoints were published immediately after every positive attempt
(CHECKPOINT-r005-001..005; 001/002 record the two failed positive attempts with
their harness defects; 003 the first full positive; 004/005 subsequent verified
runs). Failed-run artifacts are preserved occupied; every run used fresh
exclusive destinations (`ZRO117_RUNID`).

## Supported invocation / exact original037 launch recipe (unchanged from r004 except paths)

```
set ZRO117_CAPTURE_FILE=<absolute fresh capture path>
C:\AKM5C019\native-entry-adapter-018\revisions\r005\response-only-zcode.cmd --cwd D:\Projects\AK-trading <original 037 argv unchanged: session resume sess_06ac9667-3716-4c10-a5fd-dbd3310ef72d, store D:/AKZ261018/M5T7PR1/session.sqlite via Root's established env binding, unchanged model selection, single drafting prompt>
```

Array form: `node --require C:\AKM5C019\native-entry-adapter-018\revisions\r005\response-only-entry.cjs C:\Users\Administrator\.zcode\runtime\current\bin\zcode.mjs <original 037 argv...>`.
Original037 uses ENFORCEMENT mode: no `ZRO117_ASSERT_MODE`, no test seam, no
test sentinel in the environment; the envelope must observe its pre-prompt
`pre-model-turn` verdict `OK` in the claimed capture. This README is a
documentation file published after the executed R5 freeze; the freeze/manifest
records bind the executed code bytes.
