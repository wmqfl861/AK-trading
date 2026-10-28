"""Explicit offline v2 research entry point. It never loads credentials or a live feed."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
from pathlib import Path
import sys
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from ak_trading.v2.contracts import FrozenInput
from ak_trading.v2.data import read_frozen_input
from ak_trading.v2.schemas import canonical_json_bytes, decode_shared_fixture, parse_json_bytes
from ak_trading.v2.validation.experiments import (
    ExperimentError, HoldoutConsumedError, StageIncompleteError, atomic_write, code_hashes,
    digest, parse_config, run_validation_experiment, verify_payoff_training_dependencies,
    verify_reproduction,
)


def _path(text: str) -> Path:
    path = Path(text)
    if not path.is_absolute():
        path = ROOT / path
    if any(part.lower() in (".env", ".env.local", "credentials", "tokens") for part in path.parts):
        raise ExperimentError("sensitive_path_forbidden")
    if path.is_symlink():
        raise ExperimentError("symlink_input_forbidden")
    return path.resolve()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="v2离线研究与冻结影子重放")
    p.add_argument("--mode", required=True, choices=("smoke", "walk-forward", "final-holdout", "reproduce", "shadow-replay"))
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--input-bundle")
    source.add_argument("--fixture")
    p.add_argument("--config", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--feed")
    p.add_argument("--reference-run")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    output = None
    try:
        args = parser().parse_args(argv)
        if (args.mode == "shadow-replay") != (args.feed is not None):
            raise ExperimentError("feed_required_only_for_shadow_replay")
        if (args.mode == "reproduce") != (args.reference_run is not None):
            raise ExperimentError("reference_required_only_for_reproduce")
        if args.mode == "shadow-replay":
            # A4 entry guard (2026-10-26): ordinary shadow replay is still
            # engineering-incomplete (validation/shadow.py qualification
            # closure lacks exit_registry_sha256/exit_binding wiring), so a
            # fully valid request must fail fast here — after the argument
            # relationship checks, but before any path resolution, config/
            # bundle/feed/lock read, fit, registration or holdout action, and
            # before an output directory can be created. The handler below
            # returns the native stage-incomplete exit 3 with output=None.
            raise StageIncompleteError("ORDINARY_SHADOW_REPLAY_NOT_AVAILABLE")
        output = _path(args.output)
        if output.exists():
            raise ExperimentError("output_directory_exists")
        config = parse_json_bytes(_path(args.config).read_bytes())
        parse_config(config)
        # E-P1 EP1-M5 preflight: with the schema checked above, the lock's
        # existence stays the first stage report (E2 ordering), then the
        # engineering dependency gate — including the E-P1 measurement
        # interface symbols — runs before any expensive bundle reading,
        # fitting or registration. The plan-set precheck needs the frozen
        # calendar and therefore runs inside the entry, still ahead of
        # registration and every expensive fit.
        lock = Path(config["contract_lock_path"])
        if not lock.is_absolute():
            lock = ROOT / lock
        if not lock.is_file():
            raise StageIncompleteError("M1_CONTRACT_LOCK_NOT_PUBLISHED")
        verify_payoff_training_dependencies()
        if args.fixture:
            source = _path(args.fixture)
            raw = source.read_bytes()
            fixture = decode_shared_fixture(raw)
            if not config["synthetic"]:
                raise ExperimentError("shared_fixture_requires_synthetic_true")
            bundle = FrozenInput(source.parent, hashlib.sha256(raw).hexdigest(), fixture["instruments"],
                                 fixture["sessions"], fixture["bars"], fixture["macro_items"], fixture["rules"])
        else:
            bundle = read_frozen_input(_path(args.input_bundle))
        reproduction = None
        if args.mode == "reproduce":
            reproduction = verify_reproduction(_path(args.reference_run), digest(config),
                                                (bundle.manifest_sha256,), digest(code_hashes()))
        if args.mode == "shadow-replay":
            # Fully validate the frozen feed before entering the stage. Live
            # transport and credentials have no CLI representation.
            from ak_trading.v2.validation.shadow import JsonlReplayFeed, ReplayClock
            feed = JsonlReplayFeed(_path(args.feed), ReplayClock(0))
            from ak_trading.v2.validation.experiments import run_shadow_replay_experiment
            result = run_shadow_replay_experiment(bundle, config, output, feed)
        else:
            result = run_validation_experiment(bundle, config, output, args.mode)
        if reproduction is not None:
            reference = _path(args.reference_run)
            manifest = parse_json_bytes((reference / "manifest.json").read_bytes())
            for name in manifest["files"]:
                if (output / name).read_bytes() != (reference / name).read_bytes():
                    raise ExperimentError("reproduction_core_bytes_differ:" + name)
            atomic_write(output / "reproduction.json", canonical_json_bytes(reproduction))
        print(canonical_json_bytes(result).decode(), end="")
        # 06-M5 §8: exit 0 means the requested stage fully executed and its
        # report was published; statistical FAIL/INSUFFICIENT does not
        # downgrade it, and readiness["m5_complete"] (the dual-frequency
        # SHADOW_ACCEPTED batch flag) is not the single-stage completion
        # test. An engineering-FAIL readiness (replay incomplete, train or
        # calibration insufficient, payoff episodes missing) keeps the native
        # stage-incomplete exit 3; final-holdout stays closed here (exit 3).
        return 0 if result["engineering"] == "PASS" and args.mode in ("smoke", "walk-forward", "reproduce") else 3
    except SystemExit as error:
        return int(error.code)
    except HoldoutConsumedError as error:
        code = 4
        reason = str(error)
    except StageIncompleteError as error:
        code = 3
        reason = str(error)
    except (ValueError, TypeError, OSError) as error:
        code = 2
        reason = f"{type(error).__name__}:{error}"
    except Exception as error:
        code = 1
        reason = f"{type(error).__name__}:{error}"
    failure = {"mode": "ERROR", "exit_code": code, "reason": reason,
               "engineering": "FAIL", "statistics": "INSUFFICIENT", "runtime": "OFFLINE_ONLY",
               "m5_complete": False}
    if output is not None and not output.exists():
        output.mkdir(parents=True, exist_ok=False)
        atomic_write(output / "error.json", canonical_json_bytes(failure))
    print(canonical_json_bytes(failure).decode(), file=sys.stderr, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
