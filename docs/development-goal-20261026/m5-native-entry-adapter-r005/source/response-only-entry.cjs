'use strict';
// response-only-entry.cjs -- task-local preload for the native headless prompt
// entry (revision r005, task m5-native-entry-adapter-dev-020-001, author017..020).
//
// HONEST IDENTIFICATION: TASK-LOCAL MODIFIED INVOCATION CODE, not stock CLI
// functionality. Behavior is identical to r004 (mandatory exclusive fail-closed
// capture; admission; in-memory transforms T1b/T1c/T2; excluded protocol route)
// except that the deterministic pin-verification/transform operation now lives
// in the shared helper apply-transforms.cjs imported by both this preload and
// the actual-source test seam (author020 §2: same implementation, no copy).
//
// Usage: node --require <this file> <installed bin/zcode.mjs OR agent/zcode.cjs> <original argv...>
// with ZRO117_CAPTURE_FILE=<absolute fresh capture path> (mandatory).
// Fail-closed exits: 3 INSTALLED_INPUT_DRIFT, 4 TRANSFORM_COUNT_UNEXPECTED,
// 5 TRANSFORM_NOT_APPLIED, 6 UNSUPPORTED_INVOCATION, 7 CAPTURE_*.

const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const { loadPin, verifyInstalled, countAndApply } = require(path.join(__dirname, 'apply-transforms.cjs'));
const { createGuard } = require(path.join(__dirname, 'guard-core.cjs'));

const SIDE = loadPin(path.join(__dirname, 'adapter-pin.json'));
const ENTRY = path.resolve(SIDE.installed_entry.path);
const WRAPPER = SIDE.installed_wrapper && SIDE.installed_wrapper.admitted_as_entry_identity
  ? path.resolve(SIDE.installed_wrapper.path)
  : null;

const state = { transformApplied: false, guard: null, events: 0 };

function fatal(code, tag, detail) {
  try {
    process.stderr.write('[zro117] FATAL ' + tag + ' ' + String(detail).slice(0, 400) + '\n');
  } catch (_) { /* best effort */ }
  process.exit(code);
}

function realpathOrNull(p) {
  try { return fs.realpathSync(p); } catch (_) { return null; }
}

const ENTRY_REAL = realpathOrNull(ENTRY);
if (!ENTRY_REAL) fatal(3, 'INSTALLED_INPUT_DRIFT', 'realpath:' + ENTRY);
const WRAPPER_REAL = WRAPPER ? realpathOrNull(WRAPPER) : null;

// -- 1. invocation identity (before capture so identity failures keep exit 6)
const argv1 = process.argv[1] ? path.resolve(process.argv[1]) : null;
const argv1Real = argv1 ? realpathOrNull(argv1) : null;
let admittedAs = null;
if (argv1 === ENTRY || argv1 === ENTRY_REAL || argv1Real === ENTRY_REAL) admittedAs = 'entry';
else if (WRAPPER && (argv1 === WRAPPER || argv1 === WRAPPER_REAL || argv1Real === WRAPPER_REAL)) admittedAs = 'wrapper';
if (!admittedAs) fatal(6, 'UNSUPPORTED_INVOCATION', 'argv1=' + process.argv[1]);

// -- 2. MANDATORY EXCLUSIVE CAPTURE (O_EXCL claim; write failures fatal; no fallback)
const CAP = process.env.ZRO117_CAPTURE_FILE;
if (typeof CAP !== 'string' || CAP.length === 0 || !path.isAbsolute(CAP)) {
  fatal(7, 'CAPTURE_NOT_CONFIGURED', 'ZRO117_CAPTURE_FILE=' + String(CAP));
}
let capFd;
try {
  capFd = fs.openSync(CAP, 'wx');
} catch (e) {
  fatal(7, 'CAPTURE_CLAIM_FAILED', (e && e.code) + ':' + (e && e.message));
}
function emit(ev) {
  state.events += 1;
  ev.time = new Date().toISOString();
  const line = JSON.stringify(ev) + '\n';
  try {
    fs.writeSync(capFd, line);
  } catch (e) {
    try {
      process.stderr.write('[zro117] FATAL CAPTURE_WRITE_FAILED ' + (e && e.message) + '\n');
    } catch (_) { /* best effort */ }
    process.exit(7);
  }
  try { process.stderr.write('[zro117] ' + line); } catch (_) { /* mirror only */ }
}
emit({ guard: 'zro117', phase: 'invocation', argv1: process.argv[1], argv1Resolved: argv1, admittedAs, realpath: ENTRY_REAL, capture: CAP });

// -- 3. installed-input pin + counted transforms via the SHARED helper
let buf;
try {
  buf = fs.readFileSync(ENTRY);
} catch (e) {
  fatal(3, 'INSTALLED_INPUT_DRIFT', 'read:' + (e && e.message));
}
const drift = verifyInstalled(SIDE, buf);
if (drift) fatal(3, 'INSTALLED_INPUT_DRIFT', drift);
const result = countAndApply(SIDE, buf.toString('utf8'));
if (result.errors) fatal(4, 'TRANSFORM_COUNT_UNEXPECTED', result.errors.join(';'));
const src = result.src;

// -- 4. runtime guard shim (frozen; absent shim makes the transformed guard call throw)
state.guard = createGuard(emit, { assertMode: process.env.ZRO117_ASSERT_MODE });
globalThis.__ZRO117__ = Object.freeze({
  assert: state.guard.assertEmptyToolRegistry,
  revision: 'r005',
});

// -- 5. compile hook: transform the pinned entry exactly once, in memory only.
const extJs = Module._extensions['.js'];
let hooked = true;
Module._extensions['.js'] = function patchedHandler(module, filename) {
  if (hooked) {
    const resolved = path.resolve(filename);
    let match = resolved === ENTRY || resolved === ENTRY_REAL;
    if (!match && path.basename(resolved) === path.basename(ENTRY)) {
      const rf = realpathOrNull(resolved);
      match = rf !== null && rf === ENTRY_REAL;
    }
    if (match) {
      hooked = false;
      Module._extensions['.js'] = extJs;
      state.transformApplied = true;
      emit({ guard: 'zro117', phase: 'transform-applied', file: resolved, realpath: ENTRY_REAL });
      return module._compile(src, filename);
    }
  }
  return extJs.call(this, module, filename);
};

// -- 6. honest closure
process.on('exit', (code) => {
  if (!state.transformApplied) {
    try {
      process.stderr.write('[zro117] FATAL TRANSFORM_NOT_APPLIED pinned entry never compiled\n');
    } catch (_) { /* best effort */ }
    process.exitCode = 5;
    try { fs.closeSync(capFd); } catch (_) { /* best effort */ }
    return;
  }
  emit({ guard: 'zro117', phase: 'exit', code: code == null ? 0 : code, guardCalls: state.guard.state.guardCalls });
  try { fs.closeSync(capFd); } catch (_) { /* best effort */ }
});
