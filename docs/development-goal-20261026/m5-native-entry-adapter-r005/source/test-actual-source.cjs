'use strict';
// test-actual-source.cjs -- r005 actual-source seam (task m5-native-entry-adapter-dev-020-001).
//
// author020 §2/§3: NO whole-CLI bootstrap. The pinned installed bundle is read,
// pin-verified and transformed with the SAME shared helper the production
// preload uses. The relevant NATIVE units -- both supported construction
// statements, built-in registration (Soe), deferred-MCP registration (Ibn) and
// the stock dispatcher pair (nyo entry + mma with its actual ToolNotFound
// rejection branch) -- are CHECKED-EXTRACTED from the transformed bytes with
// balanced-boundary scanning (missing/ambiguous matches fail closed) and
// executed in vm sandboxes whose free variables resolve ONLY through a
// recording proxy: explicit inert dependencies plus opaque identity markers.
// Every external reference is logged; references outside the declared inert
// dependency set fail the run. Profile/auth/plugin/provider code is never
// loaded or called. Positive runs first; the checkpoint is published
// immediately after it; a failed positive stops dependent checks.

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const vm = require('node:vm');

const R005 = 'C:/AKM5C019/native-entry-adapter-018/revisions/r005';
const EVID = 'C:/AKM5C019/native-entry-adapter-018/evidence/owner';
const OUT = path.join(EVID, 'actual-source-r005');
const RUNID = process.env.ZRO117_RUNID || '001';
fs.mkdirSync(OUT, { recursive: true });

const { loadPin, verifyInstalled, countAndApply } = require(path.join(R005, 'apply-transforms.cjs'));
const { createGuard } = require(path.join(R005, 'guard-core.cjs'));

const PIN = loadPin(path.join(R005, 'adapter-pin.json'));
const TX = {};
for (const t of PIN.transforms) TX[t.id] = t;

function sha(b) { return crypto.createHash('sha256').update(b).digest('hex'); }
function claimExclusive(file, data) {
  const fd = fs.openSync(file, 'wx');
  fs.writeSync(fd, data);
  return fd;
}
function appendFd(fd, obj) {
  fs.writeSync(fd, JSON.stringify(obj) + '\n');
}

// ---------- 0. pin-verify + shared-transform the installed source ----------
const installedPath = PIN.installed_entry.path;
const buf = fs.readFileSync(installedPath);
const drift = verifyInstalled(PIN, buf);
if (drift) { console.log('FATAL INSTALLED_INPUT_DRIFT ' + drift); process.exit(3); }
const transformed = countAndApply(PIN, buf.toString('utf8'));
if (transformed.errors) { console.log('FATAL TRANSFORM_COUNT_UNEXPECTED ' + transformed.errors.join(';')); process.exit(4); }
const SRC = transformed.src;
const ORIG = buf.toString('utf8');

// ---------- 1. checked extraction (balanced, quote-aware; fail closed) ----------
function sliceBalanced(text, atChar, open, close) {
  let depth = 0, i = atChar, q = null;
  while (i < text.length) {
    const c = text[i];
    if (q) { if (c === '\\') { i += 2; continue; } if (c === q) q = null; i++; continue; }
    if (c === "'" || c === '"' || c === '`') { q = c; i++; continue; }
    if (c === open) depth += 1;
    else if (c === close) { depth -= 1; if (depth === 0) return i; }
    i += 1;
  }
  return -1;
}
function extractFn(text, header) {
  const i = text.indexOf(header);
  if (i < 0 || text.indexOf(header, i + 1) >= 0) return null;
  const brace = text.indexOf('{', i + header.length - 1);
  const end = sliceBalanced(text, brace, '{', '}');
  if (end < 0) return null;
  return { start: i, end: end + 1, text: text.slice(i, end + 1) };
}
function extractStatement(text, anchor, stmtHead) {
  const ai = text.indexOf(anchor);
  if (ai < 0 || text.indexOf(anchor, ai + 1) >= 0) return null;
  const winStart = Math.max(0, ai - 2500);
  const si = text.lastIndexOf(stmtHead, ai);
  if (si < winStart) return null;
  const openParen = text.indexOf('(', si);
  const endParen = sliceBalanced(text, openParen, '(', ')');
  if (endParen < 0) return null;
  return { start: si, end: endParen + 1, text: text.slice(si, endParen + 1) };
}

const units = {};
const provenance = { schema: 'zro117-extraction/r005-001', generated: new Date().toISOString(), installed: { path: installedPath, bytes: buf.length, sha256: sha(buf) }, units: {} };
function unit(id, u, anchor, note) {
  if (!u) { console.log('FATAL EXTRACTION_FAILED ' + id); process.exit(8); }
  units[id] = u;
  provenance.units[id] = { anchor, startChar: u.start, endChar: u.end, bytes: Buffer.byteLength(u.text), sha256: sha(Buffer.from(u.text, 'utf8')), note };
}
unit('U-siteA', extractStatement(SRC, TX['T1b-headless-construction-allowlist-A'].replace, 'I=await Ae({'), TX['T1b-headless-construction-allowlist-A'].replace, 'transformed headless construction site A statement (I=await Ae({...}))');
unit('U-siteB', extractStatement(SRC, TX['T1c-headless-construction-allowlist-B'].replace, 'jt=await je({'), TX['T1c-headless-construction-allowlist-B'].replace, 'transformed headless construction site B statement (jt=await je({...}))');
unit('U-soe', extractFn(SRC, 'function Soe(e,t={}){'), 'function Soe(e,t={}){', 'actual built-in tool registration loop (allowedTools filtering)');
unit('U-ibn', extractFn(SRC, 'function Ibn(e,t,n,o={}){'), 'function Ibn(e,t,n,o={}){', 'actual deferred MCP registration loop (allowedTools filtering)');
unit('U-nyo', extractFn(SRC, 'async function nyo(e,t,n,o){'), 'async function nyo(e,t,n,o){', 'actual dispatcher entry (registry.get lookup; name:"unknown" fallback)');
unit('U-mma', extractFn(SRC, 'async function mma(e,t,n,o,s,a){'), 'async function mma(e,t,n,o,s,a){', 'actual dispatch executor with stock ToolNotFound rejection branch');
unit('U-ffa', extractFn(SRC, 'function ffa(e,t){'), 'function ffa(e,t){', 'actual built-in registry-entry transformer invoked by Soe when registration is not filtered out');
// bind provenance to the executed freeze bytes BEFORE executing any unit
const provFd = claimExclusive(path.join(OUT, 'EXTRACTION-r005-' + RUNID + '.json'), JSON.stringify(provenance, null, 1) + '\n');
fs.closeSync(provFd);

// ---------- 2. recording inert scope ----------
const INTRINSICS = new Set(['Date', 'JSON', 'Math', 'Symbol', 'Promise', 'Object', 'Array', 'Set', 'Map', 'WeakMap', 'String', 'Number', 'Boolean', 'Error', 'TypeError', 'RangeError', 'RegExp', 'parseInt', 'parseFloat', 'isNaN', 'isFinite', 'encodeURIComponent', 'decodeURIComponent', 'structuredClone', 'Intl']);
const accessLog = [];
function inertScope(unitId, deps) {
  const proxy = new Proxy({}, {
    has: () => true,
    get: (t, prop) => {
      if (typeof prop === 'symbol') return undefined;
      accessLog.push({ unit: unitId, ref: String(prop) });
      if (Object.prototype.hasOwnProperty.call(deps, prop)) return deps[prop];
      if (INTRINSICS.has(prop)) return globalThis[prop]; // language intrinsics only
      return undefined;
    },
    set: (t, prop, v) => { accessLog.push({ unit: unitId, write: String(prop) }); return true; },
  });
  return vm.createContext(proxy);
}
function evalUnit(unitId, deps, expr) {
  const ctx = inertScope(unitId, deps);
  return vm.runInContext(expr, ctx, { timeout: 20000 });
}
function assertNoUnexpectedRefs() {
  const allowed = {
    'U-siteA': ['Ae', 'R', 'xt', 'dZo', 'z', 'o', 'a', 'l', 'f', 'g', '_', 'Ce', 'Be', 'I'],
    'U-siteB': ['je', 'qe', 'vt', 'e', 'It', 'He', 't', 'u', 'f', 'ct', 'xt', 'j', 'a', 'w', 'n', 'jt', 'fYo'],
    'U-soe': ['cbn', 'v_e', 'J7', 'ffa'],
    'U-ibn': ['v_e', 'i4', 'Vfa', 'Hfa'],
    'U-ffa': ['Xco', '_Pe', '$mt', '$_n', 'Mdo', 'opo', 'Suo'],
    'U-nyo': ['ryo', 'G_o', 'mma'],
    'U-mma': ['p1', '_A', 'ryo', 'Q_o', 'mw', 'nT', 'Gr', 'fr', 'IG', 'Qa'],
  };
  const unexpected = accessLog.filter((l) => l.ref && !INTRINSICS.has(l.ref) && !(allowed[l.unit] || []).includes(l.ref));
  return unexpected;
}

// opaque identity markers (identity must pass through the actual constructions)
const MARK = {
  authPortA: { marker: 'AUTH-PORT-A' }, authPortB: { marker: 'AUTH-PORT-B' },
  sessionA: 'sess-synthetic-A', sessionB: 'sess-synthetic-B',
  cwdA: 'C:/synthetic/wsA', cwdB: 'C:/synthetic/wsB',
  env: { marker: 'ENV' }, registrySvc: { marker: 'REGISTRY-SVC' }, modelSel: { marker: 'MODEL-SELECTION' },
  appA: { marker: 'APP-A' }, appB: { marker: 'APP-B' },
  toolNotFoundCode: 'MARK:ToolNotFound',
};

function makeRegistry() {
  const map = new Map();
  return {
    register: (name, tool) => map.set(name, tool || { name }),
    unregister: (name) => map.delete(name),
    get: (name) => map.get(name),
    toContracts: () => Array.from(map.values()),
  };
}
const SYN_BUILTINS = [
  { metadata: { name: 'Bash', description: 'syn' } },
  { metadata: { name: 'Read', description: 'syn' } },
  { metadata: { name: 'Edit', description: 'syn' } },
  { metadata: { name: 'Agent', description: 'syn' } },
  { metadata: { name: 'Glob', description: 'syn' } },
];
const SYN_MCP_TOOLS = [
  { serverName: 'synA', toolName: 'query', inputSchema: {} },
  { serverName: 'synB', toolName: 'fetch', inputSchema: {} },
];
const v_eInert = (x) => (Array.isArray(x) && x.length ? new Set(x) : undefined);

// capture writer for the seam run
const capPath = path.join(OUT, 'capture-positive-r005-' + RUNID + '.jsonl');
const capFd = claimExclusive(capPath, '');
const seamEvents = [];
function seamEmit(ev) {
  ev.time = new Date().toISOString();
  seamEvents.push(ev);
  appendFd(capFd, { guard: 'zro117-seam', ...ev });
}

const outcomes = [];
function check(id, pass, detail) {
  outcomes.push({ id, pass: !!pass, detail: detail || '' });
  console.log((pass ? 'PASS' : 'FAIL') + ' ' + id + (detail ? ' | ' + detail : ''));
  return pass;
}

// ---------- 3. POSITIVE (first) ----------
let positiveOk = true;
(async function positive() {
  try {
    // (a) actual construction site A
    let capturedA = null;
    const depsA = {
      Ae: (arg) => { capturedA = arg; return MARK.appA; },
      R: { browserControlPort: { marker: 'BC-A' } }, xt: MARK.env, dZo: () => ({ marker: 'PB' }),
      z: { runtime: { registryService: MARK.registrySvc }, configuredDefaultModelSelection: MARK.modelSel, providerRuntimeHeadersPort: MARK.authPortA },
      o: { detectedLocale: 'en', locale: 'en', enableWorkflow: true, memoryBench: false },
      a: 'syn-version', l: 'yolo', f: undefined, g: undefined, _: 'syn-surface', Ce: MARK.cwdA, Be: MARK.sessionA,
    };
    const IA = await evalUnit('U-siteA', depsA, '(async function(){var I; ' + units['U-siteA'].text + '; return I;})()');
    positiveOk = check('P1-siteA-executes', IA === MARK.appA && !!capturedA, 'returned ' + (IA === MARK.appA ? 'APP-A' : 'unexpected')) && positiveOk;
    const bindA = capturedA && Array.isArray(capturedA.runtimeConfig.toolAllowlist) && capturedA.runtimeConfig.toolAllowlist.length === 0
      && capturedA.providerRuntimeHeadersPort === MARK.authPortA
      && capturedA.sessionId === MARK.sessionA
      && capturedA.runtimeConfig.workingDirectory === MARK.cwdA
      && capturedA.env === MARK.env
      && capturedA.providerRegistry === MARK.registrySvc
      && capturedA.configuredDefaultModelSelection === MARK.modelSel;
    positiveOk = check('P2-siteA-binds-empty-and-preserves-identity', !!bindA,
      'allowlist=' + JSON.stringify(capturedA && capturedA.runtimeConfig.toolAllowlist) + ' authPortId=' + (capturedA && capturedA.providerRuntimeHeadersPort === MARK.authPortA) + ' session/cwd=' + (capturedA && capturedA.sessionId === MARK.sessionA && capturedA.runtimeConfig.workingDirectory === MARK.cwdA)) && positiveOk;
    seamEmit({ phase: 'construction', site: 'A', allowlistBound: !!(capturedA && Array.isArray(capturedA.runtimeConfig.toolAllowlist) && capturedA.runtimeConfig.toolAllowlist.length === 0), authPortPreserved: !!(capturedA && capturedA.providerRuntimeHeadersPort === MARK.authPortA) });

    // (b) actual construction site B
    let capturedB = null;
    const depsB = {
      je: (arg) => { capturedB = arg; return MARK.appB; },
      qe: { browserControlPort: { marker: 'BC-B' } }, vt: MARK.env,
      e: { projectConfigPath: 'syn-pcp', skipUserConfig: true, userConfigPath: 'syn-ucp' },
      It: { runtime: { registryService: MARK.registrySvc }, providerRuntimeHeadersPort: MARK.authPortB },
      He: MARK.modelSel, t: { override: 'yolo' }, u: undefined, f: undefined, ct: MARK.cwdB, xt: MARK.sessionB,
      j: { marker: 'PB-B' }, a: 'en', w: 'en', n: 'syn-version', fYo: { marker: 'TITLE-GEN' },
    };
    const IB = await evalUnit('U-siteB', depsB, '(async function(){var jt; ' + units['U-siteB'].text + '; return jt;})()');
    const bindB = IB === MARK.appB && capturedB
      && Array.isArray(capturedB.runtimeConfig.toolAllowlist) && capturedB.runtimeConfig.toolAllowlist.length === 0
      && capturedB.providerRuntimeHeadersPort === MARK.authPortB
      && capturedB.sessionId === MARK.sessionB
      && capturedB.runtimeConfig.workingDirectory === MARK.cwdB;
    positiveOk = check('P3-siteB-binds-empty-and-preserves-identity', !!bindB, 'allowlist=' + JSON.stringify(capturedB && capturedB.runtimeConfig.toolAllowlist) + ' authPortId=' + (capturedB && capturedB.providerRuntimeHeadersPort === MARK.authPortB)) && positiveOk;
    seamEmit({ phase: 'construction', site: 'B', allowlistBound: !!(capturedB && Array.isArray(capturedB.runtimeConfig.toolAllowlist) && capturedB.runtimeConfig.toolAllowlist.length === 0), authPortPreserved: !!(capturedB && capturedB.providerRuntimeHeadersPort === MARK.authPortB) });

    // (c) actual registration/filtering with NONEMPTY candidate populations
    const registry = makeRegistry();
    const Soe = evalUnit('U-soe', { cbn: SYN_BUILTINS, v_e: v_eInert, J7: () => false }, '(' + units['U-soe'].text + ')');
    Soe(registry, { allowedTools: capturedA.runtimeConfig.toolAllowlist, disallowedTools: undefined, includeSkill: true, includeAgent: true, includeSendMessage: true, includeSubmitResult: true, includeEscalate: true, includeWorkflow: true, includeAutomation: true, includeRespondToCoordinator: true, includeDynamicWorkflow: true, embeddedSearchEnabled: true });
    positiveOk = check('P4-soe-builtin-registration-empty', registry.toContracts().length === 0, 'registered=' + registry.toContracts().length + ' candidates=' + SYN_BUILTINS.length) && positiveOk;
    seamEmit({ phase: 'builtin-registration', candidates: SYN_BUILTINS.length, registered: registry.toContracts().length });

    const Ibn = evalUnit('U-ibn', { v_e: v_eInert, i4: (t) => 'mcp__' + t.serverName + '__' + t.toolName, Vfa: (t) => 'mcp__' + t.serverName + '__' + t.toolName, Hfa: (name, tool, port, off) => ({ name, tool, inert: true }) }, '(' + units['U-ibn'].text + ')');
    const ibnOut = Ibn(registry, { marker: 'MCP-PORT' }, SYN_MCP_TOOLS, { allowedTools: capturedA.runtimeConfig.toolAllowlist, disallowedTools: undefined });
    positiveOk = check('P5-ibn-deferred-mcp-registration-empty', registry.toContracts().length === 0 && Array.isArray(ibnOut) && ibnOut.length === 0, 'registered=' + registry.toContracts().length + ' admitted=' + (ibnOut && ibnOut.length) + ' candidates=' + SYN_MCP_TOOLS.length) && positiveOk;
    seamEmit({ phase: 'deferred-registration', candidates: SYN_MCP_TOOLS.length, registered: registry.toContracts().length });

    // (d) ACTUAL guard on a session derived from the construction result
    const guard = createGuard((ev) => seamEmit(ev), {});
    const sess = { config: { toolAllowlist: capturedA.runtimeConfig.toolAllowlist, toolDisallowlist: capturedA.runtimeConfig.toolDisallowlist }, registry };
    let guardOk = false;
    try { guard.assertEmptyToolRegistry(sess, registry.toContracts(), registry); guardOk = true; } catch (e) { guardOk = false; seamEmit({ phase: 'guard', error: e.message }); }
    positiveOk = check('P6-real-guard-ok-after-registration', guardOk && seamEvents.some((e) => e.phase === 'pre-model-turn' && e.verdict === 'OK' && e.registeredCount === 0 && e.toolCount === 0), 'guardOk=' + guardOk) && positiveOk;

    // (e) ACTUAL dispatcher rejection for an absent tool (no handler execution)
    const IGcalls = [];
    let getModeCalls = 0;
    const eSess = {
      registry, sessionId: MARK.sessionA, turnId: 'turn-syn', traceContext: { marker: 'TRC' }, model: { marker: 'MODEL' },
      logger: undefined,
      getMode: () => { getModeCalls += 1; return 'yolo'; },
    };
    const MMA = evalUnit('U-mma', {
      p1: () => ({ marker: 'TRC' }), _A: () => ({ marker: 'TRC' }), ryo: () => false,
      Q_o: (f) => f, mw: () => ({ traceId: 'trc-syn', turnId: 'turn-syn' }),
      nT: (call, err) => ({ call, error: err }),
      Gr: (code, msg, ctx) => ({ code, msg, context: ctx }),
      fr: { ToolNotFound: MARK.toolNotFoundCode, ToolCancelled: 'ToolCancelled' },
      IG: async (e, id, w, tI, err) => { IGcalls.push({ id, err }); },
      Qa: () => ({}),
    }, '(' + units['U-mma'].text + ')');
    const NYO = evalUnit('U-nyo', { ryo: () => false, G_o: (e, u, o, handler) => handler(undefined), mma: MMA }, '(' + units['U-nyo'].text + ')');
    const absentName = 'Zro117SyntheticUnavailableTool';
    const dispatchResult = await NYO(eSess, { marker: 'TURN' }, { id: 'call-syn-1', name: absentName, input: '{}' }, { marker: 'OPTS' });
    const rejected = dispatchResult && dispatchResult.error
      && dispatchResult.error.code === MARK.toolNotFoundCode
      && String(dispatchResult.error.msg).indexOf('Tool not found: ' + absentName) !== -1
      && dispatchResult.error.context && dispatchResult.error.context.recoverable === false
      && IGcalls.length === 1
      && getModeCalls === 0;
    positiveOk = check('P7-actual-unavailable-dispatch-rejection', !!rejected, 'code=' + (dispatchResult && dispatchResult.error && dispatchResult.error.code) + ' msg=' + (dispatchResult && dispatchResult.error && String(dispatchResult.error.msg).slice(0, 60)) + ' IG=' + IGcalls.length + ' getMode=' + getModeCalls) && positiveOk;
    seamEmit({ phase: 'dispatch-rejection', tool: absentName, code: dispatchResult && dispatchResult.error && dispatchResult.error.code, recoverable: dispatchResult && dispatchResult.error && dispatchResult.error.context && dispatchResult.error.context.recoverable, handlerExecuted: getModeCalls > 0 });

    // (f) zero unexpected external references (inert-dependency boundary held)
    const unexpected = assertNoUnexpectedRefs();
    positiveOk = check('P8-zero-unexpected-refs', unexpected.length === 0, 'unexpected=' + JSON.stringify(unexpected.slice(0, 8))) && positiveOk;

    // (g) controlled completion sentinel ONLY after all checks
    seamEmit({ phase: 'test-sentinel', action: 'seam-complete', positive: positiveOk, checks: outcomes.length });
  } catch (e) {
    positiveOk = false;
    seamEmit({ phase: 'seam-error', error: String(e && e.message), stack: String(e && e.stack).slice(0, 400) });
    check('P0-positive-threw', false, String(e && e.message));
  } finally {
    fs.closeSync(capFd);
  }
  // checkpoint IMMEDIATELY after the positive (author020 §4)
  const cpFd = claimExclusive(path.join(OUT, 'CHECKPOINT-r005-' + RUNID + '.json'), JSON.stringify({ schema: 'zro117-checkpoint/r005-' + RUNID, generated: new Date().toISOString(), positivePass: positiveOk, outcomes }, null, 1) + '\n');
  fs.closeSync(cpFd);
  console.log('CHECKPOINT published (positive=' + positiveOk + '): evidence/owner/actual-source-r005/CHECKPOINT-r005-' + RUNID + '.json');
  if (!positiveOk) {
    // failed positive stops dependent checks (author020 §4)
    const fd = claimExclusive(path.join(OUT, 'LOCAL-CHECKS-r005-' + RUNID + '.json'), JSON.stringify({ schema: 'zro117-local-checks/r005-' + RUNID, generated: new Date().toISOString(), stopped: 'positive-failed', outcomes }, null, 1) + '\n');
    fs.closeSync(fd);
    process.exit(1);
  }
})().then(async () => {
  if (!positiveOk) return;

  // ---------- 4. negatives (only after a successful positive) ----------
  // N1 missing binding: only T2 applied; construction sites carry no toolAllowlist
  try {
    const pinT2Only = JSON.parse(JSON.stringify(PIN)); pinT2Only.transforms = [TX['T2-pre-model-empty-registry-guard']];
    const src2 = countAndApply(pinT2Only, ORIG);
    if (src2.errors) throw new Error('t2-only transform failed');
    const uA = extractStatement(src2.src, TX['T1b-headless-construction-allowlist-A'].find, 'I=await Ae({');
    let capturedA = null;
    const depsA = { Ae: (arg) => { capturedA = arg; return {}; }, R: {}, xt: {}, dZo: () => ({}), z: { runtime: {}, providerRuntimeHeadersPort: MARK.authPortA }, o: {}, a: 1, l: 'yolo', f: undefined, g: undefined, _: 1, Ce: 'x', Be: 's' };
    await evalUnit('U-siteA', depsA, '(async function(){var I; ' + uA.text + '; return I;})()');
    const guard = createGuard(() => {}, {});
    let threw = null;
    try { guard.assertEmptyToolRegistry({ config: { toolAllowlist: capturedA.runtimeConfig.toolAllowlist } }, [], makeRegistry()); } catch (e) { threw = e.message; }
    check('N1-missing-binding-fails-allowlist-not-bound', threw === 'ZRO117_ALLOWLIST_NOT_BOUND', 'threw=' + threw + ' bound=' + JSON.stringify(capturedA.runtimeConfig.toolAllowlist));
  } catch (e) { check('N1-missing-binding-fails-allowlist-not-bound', false, String(e && e.message)); }

  // N2 registered tool hidden by an empty filtered list
  try {
    const registry = makeRegistry();
    registry.register('HiddenTool', { name: 'HiddenTool' });
    const guard = createGuard(() => {}, {});
    let threw = null;
    try { guard.assertEmptyToolRegistry({ config: { toolAllowlist: [] } }, [], registry); } catch (e) { threw = e.message; }
    check('N2-hidden-registered-tool-rejected', threw === 'ZRO117_REGISTERED_REGISTRY_NOT_EMPTY', 'threw=' + threw);
  } catch (e) { check('N2-hidden-registered-tool-rejected', false, String(e && e.message)); }

  // N3 deferred-registration counterexample: candidates DO register without the
  // empty allowlist and are filtered only by it (intended reason)
  try {
    const FFA = evalUnit('U-ffa', {
      Xco: (o) => ({ name: 'Bash', inert: o }), _Pe: ' no-match-syn', $mt: (s) => ({ name: 'submit', schema: s }),
      $_n: (o) => ({ name: 'Agent', inert: o }), Mdo: (o) => ({ name: 'Task', inert: o }),
      opo: (o) => ({ name: 'EnterPlanMode', inert: o }), Suo: (o) => ({ name: 'js', inert: o }),
    }, '(' + units['U-ffa'].text + ')');
    const Soe = evalUnit('U-soe', { cbn: SYN_BUILTINS, v_e: v_eInert, J7: () => false, ffa: FFA }, '(' + units['U-soe'].text + ')');
    const Ibn = evalUnit('U-ibn', { v_e: v_eInert, i4: (t) => 'mcp__' + t.serverName + '__' + t.toolName, Vfa: (t) => 'mcp__' + t.serverName + '__' + t.toolName, Hfa: (n) => ({ name: n }) }, '(' + units['U-ibn'].text + ')');
    const r1 = makeRegistry();
    // embeddedSearchEnabled:false so the ONLY active filter is the allowlist
    // (with it true, the actual branch rule replaces Glob/Grep via embedded
    // search -- observed in RUNID=004: 4/5 registered; real behavior, not the
    // counterexample under test).
    Soe(r1, { allowedTools: undefined, includeSkill: true, includeAgent: true, includeSendMessage: true, includeSubmitResult: true, includeEscalate: true, includeWorkflow: true, includeAutomation: true, includeRespondToCoordinator: true, includeDynamicWorkflow: true, embeddedSearchEnabled: false });
    const c1 = r1.toContracts().length;
    const r2 = makeRegistry();
    Ibn(r2, {}, SYN_MCP_TOOLS, { allowedTools: undefined });
    const c2 = r2.toContracts().length;
    const r3 = makeRegistry();
    Ibn(r3, {}, SYN_MCP_TOOLS, { allowedTools: [] });
    const c3 = r3.toContracts().length;
    check('N3-nonempty-candidates-register-without-binding', c1 === SYN_BUILTINS.length && c2 === SYN_MCP_TOOLS.length && c3 === 0,
      'soeNoAllowlist=' + c1 + '/' + SYN_BUILTINS.length + ' ibnNoAllowlist=' + c2 + '/' + SYN_MCP_TOOLS.length + ' ibnEmptyAllowlist=' + c3);
  } catch (e) { check('N3-nonempty-candidates-register-without-binding', false, String(e && e.message)); }

  // ---------- 5. capture checks rerun once against the factored r005 preload ----------
  const { spawnSync } = require('node:child_process');
  const FIX = path.join(OUT, 'capture-reruns');
  fs.mkdirSync(FIX, { recursive: true });
  const SYN_FIX = String.raw`'use strict';
function makeRegistry2(){const m=new Map();return{register:(n)=>m.set(n,{name:n}),unregister:(n)=>m.delete(n),get:(n)=>m.get(n),toContracts:()=>Array.from(m.values())};}
function t3(_a,_b){return function(){};}
function lva(_e){return null;}
function runTurn(e){e=e||{};let u=t3(e.turnTraceContext,"tools"),f=lva(e),g=e.automationCreateLimitReached?[]:f?this.getTools(e.model).filter(j=>!f.has(j.name)):this.getTools(e.model);u(),
console.log('SYNTHETIC after-guard');}
function siteA(opts){const Be=opts.sessionId,l=opts.mode,f=opts.toolDisallowlist,g=opts.midConversationSystem,o=opts,_='s',Ce=opts.workingDirectory;return{sessionId:Be,resume:Be!==void 0,runtimeConfig:{...l?{mode:l}:{},...f?{toolDisallowlist:f}:{},...g?{midConversationSystem:{mode:"force"}}:{},dynamicWorkflowEnabled:o.enableWorkflow===!0,memory:{extractionEnabled:o.memoryBench===!0},modelStreaming:"on",presentationSurface:_,workingDirectory:Ce}};}
function siteB(t,e){const xt=e.sessionId,u=t.toolDenylist,f=t.midConversationSystem,ct=e.workingDirectory;return{sessionId:xt,resume:xt!==void 0,runtimeConfig:{...t.override?{mode:t.override}:{},...u?{toolDisallowlist:u}:{},...f?{midConversationSystem:{mode:"force"}}:{},modelStreaming:"on",workingDirectory:ct}};}
function derive(b){const s={config:{toolAllowlist:b.runtimeConfig.toolAllowlist,toolDisallowlist:b.runtimeConfig.toolDisallowlist},registry:makeRegistry2()};s.getTools=function(_m){return this.registry.toContracts();};return s;}
const a=siteA({sessionId:'s-a',mode:'yolo',workingDirectory:'C:/syn/ws'});
const b=siteB({override:'yolo'},{sessionId:'s-b',workingDirectory:'C:/syn/ws'});
runTurn.call(derive(a),{});runTurn.call(derive(b),{});
console.log('SYNTHETIC completed');
`;
  function fixture(name, mutate) {
    const dir = path.join(FIX, name);
    fs.mkdirSync(dir, { recursive: true });
    for (const f of ['response-only-entry.cjs', 'guard-core.cjs', 'apply-transforms.cjs']) fs.copyFileSync(path.join(R005, f), path.join(dir, f));
    const entry = path.join(dir, 'synthetic-entry.cjs');
    fs.writeFileSync(entry, SYN_FIX);
    const pin = { schema: PIN.schema, revision: PIN.revision, installed_entry: { path: entry, bytes: fs.statSync(entry).size, sha256: sha(fs.readFileSync(entry)) }, installed_wrapper: null, transforms: PIN.transforms, exit_codes: PIN.exit_codes };
    if (mutate) mutate(dir, pin);
    fs.writeFileSync(path.join(dir, 'adapter-pin.json'), JSON.stringify(pin, null, 1));
    return dir;
  }
  function sub(node_args, env) {
    return spawnSync(process.execPath, node_args, { timeout: 60000, encoding: 'utf8', env: { ...process.env, ...env }, maxBuffer: 32 * 1024 * 1024 });
  }
  let seq = 0;
  const freshCap = () => path.join(FIX, 'cap-' + (++seq) + '-' + Date.now() + '.jsonl');
  const cr = [];
  function crCheck(id, r, predicate, detail) {
    const pass = r.status !== null && predicate(r);
    cr.push({ id, exit: r.status, pass, detail });
    check(id, pass, 'exit=' + r.status + ' ' + (detail || ''));
  }
  // K22 fixture positive with fresh exclusive capture
  {
    const dir = fixture('k22-fixture-positive');
    const cap = freshCap();
    const r = sub(['--require', path.join(dir, 'response-only-entry.cjs'), path.join(dir, 'synthetic-entry.cjs')], { ZRO117_CAPTURE_FILE: cap });
    const events = (() => { try { return fs.readFileSync(cap, 'utf8').trim().split('\n').map((l) => JSON.parse(l)); } catch (_) { return []; } })();
    const guards = events.filter((e) => e.phase === 'pre-model-turn');
    crCheck('K22-fixture-positive-exclusive-capture', r, () => r.status === 0 && guards.length === 2 && guards.every((g) => g.verdict === 'OK'), 'guards=' + guards.length);
  }
  // K19 absent capture
  {
    const dir = fixture('k19-capture-absent');
    const env = { ...process.env }; delete env.ZRO117_CAPTURE_FILE;
    const r = spawnSync(process.execPath, ['--require', path.join(dir, 'response-only-entry.cjs'), path.join(dir, 'synthetic-entry.cjs')], { timeout: 60000, encoding: 'utf8', env, maxBuffer: 32 * 1024 * 1024 });
    crCheck('K19-capture-absent-fatal', r, () => r.status === 7 && /CAPTURE_NOT_CONFIGURED/.test(r.stderr || ''), '');
  }
  // K20 occupied
  {
    const dir = fixture('k20-capture-occupied');
    const cap = freshCap(); fs.writeFileSync(cap, 'OCCUPIED\n');
    const r = sub(['--require', path.join(dir, 'response-only-entry.cjs'), path.join(dir, 'synthetic-entry.cjs')], { ZRO117_CAPTURE_FILE: cap });
    crCheck('K20-capture-occupied-fatal', r, () => r.status === 7 && /EEXIST/.test(r.stderr || '') && fs.readFileSync(cap, 'utf8') === 'OCCUPIED\n', '');
  }
  // K18 directory target
  {
    const dir = fixture('k18-capture-dir');
    const target = path.join(FIX, 'k18-target-dir'); fs.mkdirSync(target, { recursive: true });
    const r = sub(['--require', path.join(dir, 'response-only-entry.cjs'), path.join(dir, 'synthetic-entry.cjs')], { ZRO117_CAPTURE_FILE: target });
    crCheck('K18-capture-dir-fatal', r, () => r.status === 7 && /CAPTURE_CLAIM_FAILED/.test(r.stderr || ''), '');
  }
  // K18b missing parent
  {
    const dir = fixture('k18b-capture-missing-parent');
    const r = sub(['--require', path.join(dir, 'response-only-entry.cjs'), path.join(dir, 'synthetic-entry.cjs')], { ZRO117_CAPTURE_FILE: path.join(FIX, 'k18b-no-parent', 'cap.jsonl') });
    crCheck('K18b-capture-missing-parent-fatal', r, () => r.status === 7 && /ENOENT/.test(r.stderr || ''), '');
  }
  outcomes.push(...cr);

  // ---------- 6. summary ----------
  const failed = outcomes.filter((o) => !o.pass);
  const fd = claimExclusive(path.join(OUT, 'LOCAL-CHECKS-r005-' + RUNID + '.json'), JSON.stringify({ schema: 'zro117-local-checks/r005-' + RUNID, generated: new Date().toISOString(), node: process.version, order: 'actual-source positive first; checkpoint after positive; negatives + capture reruns after', outcomes, unexpectedRefs: assertNoUnexpectedRefs().slice(0, 20) }, null, 1) + '\n');
  fs.closeSync(fd);
  console.log('');
  console.log('SUMMARY r005: ' + (outcomes.length - failed.length) + '/' + outcomes.length + ' PASS' + (failed.length ? '; FAILED: ' + failed.map((f) => f.id).join(',') : ''));
  process.exit(failed.length ? 1 : 0);
});
