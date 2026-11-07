"""H3 signature contract: replay calls the qualification verifier strictly.

``replay.py`` is NOT edited. The verifier name imported by replay is
monkeypatched with a sentinel that binds every observed call against the REAL
``verify_qualification_evidence`` signature, so a callsite missing either
required keyword (``exit_registry_sha256`` or ``exit_binding``) fails even
though replay catches ``TypeError``: the qualification is then routed to
invalid and the SHADOW open is refused, never silently admitted.
"""
from __future__ import annotations

import inspect

from test_v2_m4_ledger import NS
from test_v2_m4_replay import _qualification_chain, _run_shadow, _shadow_setup

_TOKENS = ("QUALIFICATION_MISMATCH", "QUALIFICATION_EVIDENCE_MISSING",
           "QUALIFICATION_EVIDENCE_INVALID", "QUALIFICATION_EXPIRED",
           "STATISTICS_NOT_PASSED")


def _admission(result):
    return next(d for d in result.decisions if d.decision_nano == 100 * NS)


def test_replay_passes_both_required_verifier_keywords(monkeypatch):
    import ak_trading.v2.artifacts as artifacts
    import ak_trading.v2.decision.replay as replay_module

    real = artifacts.verify_qualification_evidence
    real_signature = inspect.signature(real)
    assert "exit_registry_sha256" in real_signature.parameters
    assert "exit_binding" in real_signature.parameters
    for keyword in ("exit_registry_sha256", "exit_binding"):
        parameter = real_signature.parameters[keyword]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty

    observed = []

    def strict_verifier(*args, **kwargs):
        bound = real_signature.bind(*args, **kwargs)  # TypeError if a keyword is missing
        bound.apply_defaults()
        observed.append({
            "keywords": sorted(kwargs),
            "exit_registry_sha256": kwargs.get("exit_registry_sha256"),
            "exit_binding": kwargs.get("exit_binding"),
            "bound_arguments": sorted(bound.arguments),
        })
        return real(*args, **kwargs)

    monkeypatch.setattr(replay_module, "verify_qualification_evidence", strict_verifier)
    (c, bundle, models, reference, pm, _bound,
     exit_specs, bindings, exit_registry, model_hashes) = _shadow_setup(monkeypatch)
    evidence, qualification, contract_lock, registry, *_ = _qualification_chain(
        c.spec, exit_registry=exit_registry, binding=bindings[0], model_hashes=model_hashes,
        issued_at_nano=100 * NS, valid_until_nano=1000 * NS)
    result = _run_shadow(c, bundle, models, reference, pm, (qualification,), evidence,
                         exit_specs, bindings, contract_lock, start=100 * NS, end=102 * NS)

    assert observed, "the replay callsite must invoke the verifier"
    for entry in observed:
        # Both required keywords present on the actual call from replay.
        assert {"exit_registry_sha256", "exit_binding"} <= set(entry["keywords"])
        assert entry["exit_registry_sha256"] == exit_registry
        assert entry["exit_binding"] is bindings[0]
        # The observed call binds cleanly against the real callable signature.
        assert entry["bound_arguments"] == sorted(real_signature.parameters)
    # And the call genuinely succeeded: the qualification admitted the open.
    decision = _admission(result)
    assert decision.selected_proposal_id is not None
    assert decision.qualification_ids == (qualification.qualification_id,)
    assert decision.weights["CASH"] == 0.0
    assert not [r for d in result.decisions for g in d.gates for r in g.reasons if r in _TOKENS]


def test_verifier_signature_failure_is_not_silently_swallowed(monkeypatch):
    """Negative control: a verifier invocation missing a required keyword
    (reproduced by dropping ``exit_binding`` before binding against the real
    signature) raises exactly the ``TypeError`` a strict verifier raises for
    an incomplete call. Replay's ``except (ValueError, TypeError)`` must route
    that to an invalid qualification — gate refused, CASH selected, zero
    intents — never a silent admission."""
    import ak_trading.v2.artifacts as artifacts
    import ak_trading.v2.decision.replay as replay_module

    real_signature = inspect.signature(artifacts.verify_qualification_evidence)

    def keyword_dropping_verifier(*args, **kwargs):
        incomplete = {name: value for name, value in kwargs.items()
                      if name != "exit_binding"}
        real_signature.bind(*args, **incomplete)  # TypeError: keyword missing
        raise AssertionError("dropping a required keyword must fail the bind")

    monkeypatch.setattr(replay_module, "verify_qualification_evidence",
                        keyword_dropping_verifier)
    (c, bundle, models, reference, pm, _bound,
     exit_specs, bindings, exit_registry, model_hashes) = _shadow_setup(monkeypatch)
    evidence, qualification, contract_lock, registry, *_ = _qualification_chain(
        c.spec, exit_registry=exit_registry, binding=bindings[0], model_hashes=model_hashes,
        issued_at_nano=100 * NS, valid_until_nano=1000 * NS)
    # The chain itself is complete and real-verifier-acceptable (positive
    # control), so the failure below can only come from the sentinel's
    # missing-keyword rejection, not from missing evidence bytes.
    artifacts.verify_qualification_evidence(
        qualification, c.spec, evidence, contract_lock, registry,
        exit_registry_sha256=exit_registry, exit_binding=bindings[0])

    result = _run_shadow(c, bundle, models, reference, pm, (qualification,), evidence,
                         exit_specs, bindings, contract_lock, start=100 * NS, end=102 * NS)
    decision = _admission(result)
    gate = decision.gates[0]
    assert not gate.eligible
    assert {"QUALIFICATION_MISMATCH", "QUALIFICATION_EVIDENCE_INVALID"} <= set(gate.reasons)
    assert decision.selected_proposal_id is None
    assert decision.weights["CASH"] == 1.0
    assert decision.weights[decision.proposals[0].proposal_id] == 0.0
    assert result.intents == () and result.fills == ()
