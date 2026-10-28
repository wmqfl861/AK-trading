'use strict';
// guard-core.cjs -- shared guard logic for the task-local response-only entry
// adapter (revision r003, tasks m5-native-entry-adapter-dev-018-00x/019-001).
//
// HONEST IDENTIFICATION (author017/019): task-local modified invocation code,
// not stock CLI functionality. Required by response-only-entry.cjs (production
// preload) and test-adapter.cjs (checks run the real guard, not a duplicate).
//
// r003 changes (author019 §2/§3): the guard now verifies BOTH the model-callable
// list AND the registered tool registry (a filter that empties the callable list
// cannot mask registered dispatchable tools), fails closed when the registry API
// is unrecognized, probes unavailable-tool dispatch against the real registry
// lookup used by the stock dispatcher, and supports an explicit test sentinel
// mode that terminates the process after the first verified-OK guard call,
// BEFORE any provider request. Default mode remains enforce-and-throw.

function createGuard(emit, options) {
  if (typeof emit !== 'function') throw new TypeError('guard requires emit()');
  const opts = options || {};
  const assertMode = opts.assertMode === 'verify-once-then-exit' ? 'verify-once-then-exit' : 'enforce';
  const state = { guardCalls: 0 };

  function countRegistry(registry) {
    try {
      if (registry && typeof registry.toContracts === 'function') {
        const c = registry.toContracts();
        if (Array.isArray(c)) return { ok: true, count: c.length, api: 'toContracts' };
      }
      if (registry && typeof registry.list === 'function') {
        const c = registry.list();
        if (Array.isArray(c)) return { ok: true, count: c.length, api: 'list' };
      }
    } catch (e) {
      return { ok: false, count: -1, api: 'error:' + (e && e.message) };
    }
    return { ok: false, count: -1, api: 'unrecognized' };
  }

  function dispatchProbe(registry) {
    // Unavailable-tool dispatch precondition on the real registry: the stock
    // dispatcher resolves a model tool call via registry.get(name) and fails
    // closed (ToolNotFound, recoverable:false) when the lookup is falsy.
    const name = 'Zro117SyntheticUnavailableTool';
    try {
      if (!registry || typeof registry.get !== 'function') {
        return { name, result: 'REGISTRY_GET_UNAVAILABLE' };
      }
      const found = registry.get(name);
      return found ? { name, result: 'UNEXPECTEDLY_REGISTERED' } : { name, result: 'NOT_REGISTERED' };
    } catch (e) {
      return { name, result: 'PROBE_ERROR:' + (e && e.message) };
    }
  }

  function assertEmptyToolRegistry(sess, tools, registry) {
    state.guardCalls += 1;
    const cfg = sess && typeof sess === 'object' ? sess.config : null;
    const al = cfg && typeof cfg === 'object' ? cfg.toolAllowlist : undefined;
    const allowlistBound = Array.isArray(al) && al.length === 0;
    const registryEmpty = Array.isArray(tools) && tools.length === 0;
    const reg = countRegistry(registry);
    let verdict = 'OK';
    if (!allowlistBound) verdict = 'ALLOWLIST_NOT_BOUND';
    if (!registryEmpty) verdict = verdict === 'OK' ? 'TOOL_REGISTRY_NOT_EMPTY' : verdict + '+TOOL_REGISTRY_NOT_EMPTY';
    if (!reg.ok) verdict = verdict === 'OK' ? 'REGISTRY_CHECK_FAILED' : verdict + '+REGISTRY_CHECK_FAILED';
    else if (reg.count !== 0) verdict = verdict === 'OK' ? 'REGISTERED_REGISTRY_NOT_EMPTY' : verdict + '+REGISTERED_REGISTRY_NOT_EMPTY';
    const ev = {
      guard: 'zro117',
      phase: 'pre-model-turn',
      call: state.guardCalls,
      allowlistBound,
      registryEmpty,
      toolCount: Array.isArray(tools) ? tools.length : -1,
      toolNames: Array.isArray(tools)
        ? tools.slice(0, 20).map((x) => (x && typeof x === 'object' ? String(x.name) : String(x)))
        : null,
      registeredCount: reg.count,
      registeredApi: reg.api,
      verdict,
    };
    emit(ev);
    if (verdict !== 'OK') {
      const err = new Error('ZRO117_' + verdict);
      err.zro117Event = ev;
      throw err;
    }
    if (assertMode === 'verify-once-then-exit') {
      const probe = dispatchProbe(registry);
      emit({ guard: 'zro117', phase: 'dispatch-probe', probe });
      emit({ guard: 'zro117', phase: 'test-sentinel', action: 'exit0-before-provider-request', guardCalls: state.guardCalls });
      process.exit(0);
    }
    return true;
  }

  return { assertEmptyToolRegistry, state };
}

module.exports = { createGuard };
