"""A4 finite entry guard (2026-10-26): ordinary shadow replay is rejected at the
CLI boundary. Per PHASE0-PLAN.md §6 a complete, syntactically valid shadow-replay
request must fail with the native stage-incomplete exit 3 before any path
resolution, config/bundle/feed/lock read, fit, registration or holdout action,
and before an output directory is created; argument relationship errors keep
their original exit 2. This is the guard slice only — it does not close the full
ordinary-shadow contract (P0-2 shadow / P1-2 stay open)."""
from __future__ import annotations

from pathlib import Path
import importlib.util
import json
import os
import subprocess
import sys

import pytest

from ak_trading.v2.validation import experiments as _experiments
from ak_trading.v2.validation import shadow as _shadow

ROOT=Path(__file__).resolve().parents[1]
REAL_FIXTURE=ROOT/"tests/fixtures/v2/shared.json"
REAL_CONFIG=ROOT/"tests/fixtures/v2/m5_research.json"
REAL_FEED=ROOT/"tests/fixtures/v2/m5_stream.jsonl"
GUARD_REASON="ORDINARY_SHADOW_REPLAY_NOT_AVAILABLE"
FAILURE={"mode":"ERROR","exit_code":3,"reason":GUARD_REASON,"engineering":"FAIL",
         "statistics":"INSUFFICIENT","runtime":"OFFLINE_ONLY","m5_complete":False}


def cli():
    spec=importlib.util.spec_from_file_location("m5_shadow_guard_cli",ROOT/"scripts/research_probability_v2.py")
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def shadow_argv(tmp_path,*,fixture=None,config=None,feed=None,output="run"):
    return ["--mode","shadow-replay","--fixture",str(fixture or tmp_path/"missing-fixture.json"),
            "--config",str(config or tmp_path/"missing-config.json"),
            "--output",str(tmp_path/output),"--feed",str(feed or tmp_path/"missing-feed.jsonl")]


def test_full_valid_request_is_rejected_early_with_structured_reason(tmp_path,capsys):
    module=cli()
    assert module.main(shadow_argv(tmp_path,fixture=REAL_FIXTURE,config=REAL_CONFIG,feed=REAL_FEED))==3
    captured=capsys.readouterr()
    assert captured.out==""
    assert json.loads(captured.err)==FAILURE
    # nothing was resolved or created anywhere under the scratch directory
    assert not (tmp_path/"run").exists() and list(tmp_path.iterdir())==[]


def test_guard_precedes_every_read_and_pipeline_entry(tmp_path,monkeypatch):
    module=cli()
    calls=[]
    def forbidden(name):
        def record(*args,**kwargs):
            calls.append(name);raise AssertionError("forbidden:"+name)
        return record
    # names bound into the CLI module at import time
    for name in ("_path","parse_json_bytes","parse_config","decode_shared_fixture",
                 "read_frozen_input","verify_payoff_training_dependencies",
                 "run_validation_experiment"):
        monkeypatch.setattr(module,name,forbidden(name))
    # names the shadow branch imports lazily from their source modules
    monkeypatch.setattr(_experiments,"run_shadow_replay_experiment",forbidden("run_shadow_replay_experiment"))
    monkeypatch.setattr(_shadow,"JsonlReplayFeed",forbidden("JsonlReplayFeed"))
    # every input path is absent: any real read would surface as exit 2, not 3
    assert module.main(shadow_argv(tmp_path))==3
    assert calls==[]


def test_relationship_and_parser_errors_keep_native_exit_2(tmp_path,capsys):
    module=cli()
    common=["--fixture",str(REAL_FIXTURE),"--config",str(REAL_CONFIG),"--output",str(tmp_path/"s")]
    assert module.main(["--mode","shadow-replay",*common])==2
    assert json.loads(capsys.readouterr().err)["reason"]=="ExperimentError:feed_required_only_for_shadow_replay"
    assert module.main(["--mode","shadow-replay",*common,"--feed",str(REAL_FEED),
                        "--reference-run",str(tmp_path/"ref")])==2
    assert json.loads(capsys.readouterr().err)["reason"]=="ExperimentError:reference_required_only_for_reproduce"
    assert module.main(["--mode","reproduce",*common,"--reference-run",str(tmp_path/"ref"),
                        "--feed",str(REAL_FEED)])==2
    assert module.main(["--mode","live",*common])==2
    assert module.main(["--mode","shadow-replay","--fixture",str(REAL_FIXTURE),
                        "--output",str(tmp_path/"x"),"--feed",str(REAL_FEED)])==2
    capsys.readouterr()
    assert list(tmp_path.iterdir())==[]


def test_guard_precedes_existing_output_directory_check(tmp_path):
    module=cli()
    existing=tmp_path/"run";existing.mkdir()
    assert module.main(shadow_argv(tmp_path,fixture=REAL_FIXTURE,config=REAL_CONFIG,feed=REAL_FEED))==3
    # the error handler never learned the output path, so nothing was written
    assert list(existing.iterdir())==[]


def test_normal_modes_keep_their_original_pipeline(tmp_path):
    module=cli()
    out=tmp_path/"smoke"
    assert module.main(["--mode","smoke","--fixture",str(REAL_FIXTURE),"--config",str(tmp_path/"cfg.json"),
                        "--output",str(out)])==2
    error=json.loads((out/"error.json").read_bytes())
    assert error["reason"].startswith("FileNotFoundError:")
    assert error["exit_code"]==2


def test_real_cli_process_exits_three_without_outputs(tmp_path):
    completed=subprocess.run([sys.executable,"-B",str(ROOT/"scripts/research_probability_v2.py"),
                              *shadow_argv(tmp_path,fixture=REAL_FIXTURE,config=REAL_CONFIG,feed=REAL_FEED)],
                             cwd=tmp_path,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                             encoding="utf-8",env=dict(os.environ),timeout=300)
    assert completed.returncode==3
    assert completed.stdout==""
    assert json.loads(completed.stderr)==FAILURE
    assert not (tmp_path/"run").exists() and list(tmp_path.iterdir())==[]
