"""Read-only forward paper entry point; preflight/report are entirely offline."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ak_trading.v2.artifacts import canonical_json_bytes, sha256_hex
from ak_trading.v2.schemas import parse_json_bytes
from ak_trading.v2.validation.forward_paper import (
    SCHEMA, ForwardPaperError, ForwardPaperHost, Journal, load_activation, preflight, report_from_prefix, verified_file,
)
from ak_trading.v2.validation.forward_paper_feed import (
    ResourceGuard, TqReadOnlyFeed, clock_from_binding, open_readonly_api, validate_runtime_binding,
)


def parser():
    result = argparse.ArgumentParser(description="Bounded read-only RB minute forward paper research")
    result.add_argument("mode", choices=("preflight", "capture", "run", "resume", "report"))
    result.add_argument("--config", type=Path, default=ROOT / "config/research-v2-forward-paper-rb-minute.json")
    result.add_argument("--binding", type=Path)
    result.add_argument("--run-dir", type=Path)
    result.add_argument("--wall-seconds", type=int)
    return result


def _read(path: Path):
    if path is None or not path.is_file() or path.is_symlink() or path.name.lower().startswith(".env"):
        raise ForwardPaperError("REQUIRED_INPUT_MISSING")
    value = parse_json_bytes(path.read_bytes())
    if not isinstance(value, dict):
        raise ForwardPaperError("JSON_OBJECT_REQUIRED")
    return value


def _report(root: Path):
    identity = _read(root / "IDENTITY.json")
    journal = Journal(root, identity, create=False)
    report = report_from_prefix(journal)
    path = journal.report(report)
    return {"status": "REPORT_WRITTEN", "report": str(path), "sealed_sequence": journal.sequence}


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.mode == "report":
            if args.run_dir is None:
                raise ForwardPaperError("RUN_DIRECTORY_REQUIRED")
            result = _report(args.run_dir)
            print(json.dumps(result, sort_keys=True))
            return 0
        config, binding = _read(args.config), _read(args.binding)
        activate = args.mode == "run" or (args.mode == "resume" and binding.get("activation") is not None)
        status = preflight(config, binding, activate=activate)
        if args.mode == "preflight":
            print(json.dumps(status, sort_keys=True))
            return 0 if status["requested_mode_ready"] else 2
        validate_runtime_binding(config, binding, activate=activate)
        # Only the documented run directory can be written by the live runner.
        run_id = binding["run_id"]
        expected = ROOT / "data/research_runs/v2/forward-paper" / run_id
        output = expected if args.run_dir is None else args.run_dir.resolve()
        if output.resolve() != expected.resolve() or output.is_symlink():
            raise ForwardPaperError("RUN_DIRECTORY_OUTSIDE_AUTHORIZED_ROOT")
        ordinal = binding.get("run_ordinal", 1)
        if type(ordinal) is not int or ordinal < 1:
            raise ForwardPaperError("RUN_ORDINAL_INVALID")
        duration = args.wall_seconds if args.wall_seconds is not None else (600 if ordinal == 1 else 3600)
        guard = ResourceGuard(output, duration, first=ordinal == 1)
        clock = clock_from_binding(binding)
        activation = load_activation(verified_file(binding["activation"])) if activate else None
        if activation is not None and (str(activation.initial_equity_cny) != config["initial_equity_cny"]
                                      or any(p.seed != config["seed"] for p in activation.run_policies)):
            raise ForwardPaperError("ACTIVATION_EQUITY_OR_SEED_MISMATCH")
        host = ForwardPaperHost(output, run_id, clock=clock, activation=activation, synthetic=False,
            binding_identity={"binding_sha256": sha256_hex(args.binding.read_bytes()),
                              "config_sha256": sha256_hex(canonical_json_bytes(config))}, resume=args.mode == "resume")
        try:
            api = open_readonly_api(config, binding, activate=activate)
        except ForwardPaperError as exc:
            host.facts.append({"kind": "SDK_START_RESULT", "sdk_constructed": 0, "reason": str(exc)})
            host.finish(str(exc))
            print(json.dumps({"status": str(exc), "forward_bar_count": 0, "sdk_order_calls": 0}))
            return 2
        feed = TqReadOnlyFeed(api, host, guard, expected_symbol=binding.get("expected_raw_symbol"))
        native, reason = feed.run()
        print(json.dumps({"status": reason, "native_result": native, "forward_bar_count": host.forward_bar_count,
                          "strategy_evaluation": "EVALUATED" if host.strategy_batches else "NOT_RUN",
                          "runtime_result": feed.runtime_result,
                          "sdk_order_calls": 0, "broker": "NOT_RUN", "enable_trading": False}))
        return native
    except (OSError, ValueError, TypeError, KeyError) as exc:
        # Do not serialize arbitrary decoder, filesystem or SDK exception text.
        code = str(exc) if isinstance(exc, ForwardPaperError) else "INPUT_OR_RUNTIME_FAILED"
        print(json.dumps({"status": code, "sdk_order_calls": 0, "enable_trading": False, "broker": "NOT_RUN"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
