"""SYNTHETIC K46 cases: no market services, accepted history, or replay calls."""
from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("k46_test_subject", ROOT / "tools/experiments/v1_daily_baseline_universe_v2.py")
k = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = k
SPEC.loader.exec_module(k)


def bar(product, instance_number, j, day, close):
    exchange, code = product.split(".")
    symbol = exchange + "." + code.lower() + str(2001 + instance_number)
    ts = day + "T15:00:00+08:00"
    return dict(symbol=symbol, contract_instance_id=symbol + "|SYNTHETIC", j=j,
                datetime_nano=k.timestamp_nano(ts), raw_ts=ts, raw_date=day,
                close=Fraction(close), original_ts=ts, original_trading_day=day)


def scenario(product="SHFE.RB"):
    days = ("2020-12-29", "2020-12-30", "2020-12-31", "2021-01-04")
    if product == "GFEX.SI":
        days = ("2022-12-22", "2022-12-23", "2022-12-26", "2023-01-03")
    bars = {}
    for number in (0, 1):
        records = [bar(product, number, j, day, 100 + 100 * number + j) for j, day in enumerate(days)]
        bars[records[0]["contract_instance_id"]] = records
    instances = list(bars)
    selection = [dict(raw_date=day,key=None,denominator=None,prior=None) for day in days]
    for j, inst in ((1,instances[0]),(2,instances[1]),(3,instances[0])):
        previous = bars[inst][j-1]
        selection[j].update(key=k.identity(bars[inst][j]),denominator=previous["close"]*k.MULTIPLIERS[product],prior=previous)
    accounts = {m:{} for m in k.METHODS}
    native = {}
    for method in k.METHODS:
        for inst, observations in bars.items():
            ledger, decisions, fills = [], [], []
            for j,b in enumerate(observations):
                is_control = method not in k.STRATEGIES
                opened = method != "flat_control" and j == (0 if method=="buy_hold_control" else 1)
                terminal = method != "flat_control" and j==len(observations)-1
                row = dict(capital="1000000",scenario="BASE",symbol=b["symbol"],contract_instance_id=inst,method=method,j=j,
                           datetime_nano=b["datetime_nano"],raw_ts=b["raw_ts"],raw_date=b["raw_date"],raw_year=int(b["raw_date"][:4]),
                           primary=False,terminal=j==len(observations)-1,previous_position=0,position=0,
                           execution_status="opened" if opened else "target_unchanged",opening_attempts=int(opened),opening_fills=int(opened),
                           opening_blocks=0,opening_margin_blocked=0,opening_costs_exhaust_equity=0,margin_breach_observations=0,
                           nonpositive_equity_observations=0,terminal_close_fills=int(terminal),fills=int(opened)+int(terminal))
                for name in ("cash","realized_pnl","unrealized_pnl","previous_equity","equity","gross","fee","slippage","net","gross_delta","fee_delta","slippage_delta","net_delta","terminal_close_fee_delta","terminal_close_slippage_delta"):
                    row[name] = "0"
                row["net_delta"] = "0" if method=="flat_control" else str((0,3,-5,7)[j])
                state = "warmup" if j==0 or method=="ma_trend" and j<3 else "unavailable"
                candidate = None if state=="warmup" else 0
                decision=dict(observation_index=j,datetime_nano=b["datetime_nano"],raw_ts=b["raw_ts"],execution_status=row["execution_status"],
                              opening_required_margin=None,opening_equity_after_costs=None,preceding_signal_index=j-1 if j else None,
                              evaluator_candidate_direction=candidate,strategy_status=state,reason="insufficient_history" if state=="warmup" else "flat",candidate_direction=candidate)
                if is_control:
                    for name in ("evaluator_candidate_direction","strategy_status","reason","preceding_signal_index"):
                        decision.pop(name)
                    decision["signal_index"]=None
                ledger.append(row)
                decisions.append(decision)
                for offset,phase in (("open","first_open" if method=="buy_hold_control" else "next_open"),) if opened else ():
                    fills.append(fill(b,j,offset,phase,len(fills)))
                if terminal:
                    fills.append(fill(b,j,"close","terminal_close",len(fills)))
            raw = {"ledger.csv":k.csv_bytes(ledger),"decisions.csv":k.csv_bytes(decisions),"fills.csv":k.csv_bytes(fills) if fills else k.csv_bytes([fill(observations[0],0,"open","first_open",0)]).split(b"\n")[0]+b"\n"}
            accounts[method][inst] = k.validate_account(product,method,inst,observations,*(k.rows(raw[name]) for name in ("ledger.csv","decisions.csv","fills.csv")))
            native[(inst,method)] = raw
    return bars,selection,accounts,native


def fill(b,j,offset,phase,serial):
    return dict(fill_id="f"+str(serial),observation_index=j,signal_index=j-1 if phase=="next_open" else None,
                datetime_nano=b["datetime_nano"],raw_ts=b["raw_ts"],phase=phase,offset=offset,side=1 if offset=="open" else -1,lots=1,
                ideal_price="100",effective_price="100",fee_per_lot_component="0",fee_rate_component="0",fee="0",slippage="0",realized_pnl="0",cash_after="1000000",position_after=1 if offset=="open" else 0)


@pytest.mark.parametrize("numerator,denominator,expected",[(1,2,0),(3,2,2),(5,2,2),(-1,2,0),(-3,2,-2),(-5,2,-2),(7,3,2),(-8,3,-3),(10**40+1,2,5*10**39)])
def test_signed_integer_half_even_exact(numerator,denominator,expected):
    assert k.half_even(Fraction(numerator,denominator))==expected


@pytest.mark.parametrize("value",["NaN","Infinity","-Infinity",True,0.1])
def test_nonfinite_or_float_amount_refused(value):
    with pytest.raises(k.Rejected):k.amount(value)


@pytest.mark.parametrize("value",["-1","0","1"])
def test_raw_evaluator_candidate_observation_only_and_max_age(value):
    assert k.readiness(dict(evaluator_candidate_direction=value,candidate_direction="0",strategy_status="unavailable",reason="max_signal_age_reached"))=="EVALUATOR_READY"


@pytest.mark.parametrize("status,reason,expected",[("warmup","insufficient_history","WARMUP"),("unavailable","zero_variance_window","ZERO_VARIANCE"),("unavailable","max_signal_age_reached","OTHER_UNAVAILABLE")])
def test_missing_evaluator_preserves_original_status_reason(status,reason,expected):
    assert k.readiness(dict(evaluator_candidate_direction="",candidate_direction="0",strategy_status=status,reason=reason))==expected


@pytest.mark.parametrize("candidate,status,reason",[("2","unavailable","flat"),("0","warmup","insufficient_history"),("1","unavailable","zero_variance_window"),("","ready","ready"),("","warmup","zero_variance_window"),(True,"unavailable","flat")])
def test_contradictory_readiness_refused(candidate,status,reason):
    with pytest.raises(k.Rejected):k.readiness(dict(evaluator_candidate_direction=candidate,strategy_status=status,reason=reason))


def test_fifty_percent_boundary_and_no_coverage():
    assert k.eligible(49,100)=="EXCLUDED_COVERAGE_LT_50"
    assert k.eligible(50,100)=="INCLUDED"
    assert k.eligible(0,100,False)=="INCLUDED"
    assert k.eligible(0,0)=="NO_COVERAGE"


def test_same_instance_previous_close_and_cross_year_exact_sums():
    bars,selection,accounts,_=scenario()
    summary,annual,ready,daily,audit=k.product_tables("SHFE.RB",selection,accounts)
    first=summary[0]
    expected=k.half_even(Fraction(3*k.SCALE,1000))+k.half_even(Fraction(-5*k.SCALE,2010))+k.half_even(Fraction(7*k.SCALE,1020))
    assert first["normalized_units"]==expected
    assert first["net_yuan"]==5
    assert sum(r["normalized_units"] for r in annual if r["method"]==k.METHODS[0] and r["normalized_units"] is not None)==expected
    joined={row["raw_date"]:row for row in audit if row["method"]==k.METHODS[0]}
    assert joined["2020-12-31"]["prior_close"]==201
    assert joined["2021-01-04"]["prior_gap_days"]==4
    assert annual[0]["net_yuan"]==-2 and annual[1]["net_yuan"]==7
    assert summary[1]["eligibility"]=="EXCLUDED_COVERAGE_LT_50"
    assert summary[3]["readiness_percent"] is None
    assert daily[k.METHODS[0]]["2020-12-29"] is None


def test_all_three_never_open_counts_and_buy_hold_initial_open():
    bars,selection,accounts,_=scenario()
    inst=list(bars)[1]
    # This account opens before it is selected. It has a position history but
    # has no opening event on its selected day.
    result=k.product_tables("SHFE.RB",selection,accounts)[2]
    rule=result[0]
    assert rule["all_contracts_never_opened"]==0
    assert rule["selected_contracts_never_opened"]==0
    assert rule["selected_contracts_without_selected_day_open"]==1
    assert rule["selected_opening_fills"]==1
    hold=result[4]
    assert hold["all_opening_fills"]==2 and hold["selected_opening_fills"]==0


@pytest.mark.parametrize("mutation",["wrong_fill_day","wrong_signal","duplicate_fill","wrong_instance","missing_row","extra_row","wrong_date","nonfinite","holdout"])
def test_account_join_and_event_corruption_rejected(mutation):
    bars,selection,accounts,native=scenario()
    inst=next(iter(bars));method=k.METHODS[0]
    ledger,decisions,fills=[k.rows(native[(inst,method)][name]) for name in ("ledger.csv","decisions.csv","fills.csv")]
    if mutation=="wrong_fill_day":fills[0]["observation_index"]="2"
    elif mutation=="wrong_signal":fills[0]["signal_index"]="1"
    elif mutation=="duplicate_fill":fills.append(dict(fills[0]))
    elif mutation=="wrong_instance":ledger[1]["contract_instance_id"]="different"
    elif mutation=="missing_row":ledger.pop()
    elif mutation=="extra_row":decisions.append(dict(decisions[-1]))
    elif mutation=="wrong_date":ledger[1]["raw_date"]="2020-12-31"
    elif mutation=="nonfinite":ledger[1]["net_delta"]="NaN"
    else:decisions[1]["raw_ts"]="2026-09-05T15:00:00+08:00"
    with pytest.raises(k.Rejected):k.validate_account("SHFE.RB",method,inst,bars[inst],ledger,decisions,fills)


def test_curves_start_at_zero_year_reset_and_no_selection_underwater():
    result=k.curve([-2,None,1,1,-4])
    assert result["maximum"]==4 and result["longest"]==3 and result["open_at_end"]
    assert result["daily"][1]==dict(cumulative=-2,drawdown=2,duration=2)
    assert k.curve([1])["maximum"]==0
    assert k.curve([None,None])["total"] is None


def test_combination_exclusion_si_absence_zero_participants_and_rounding():
    summaries=[];daily={}
    for product in k.PRODUCTS:
        _,selection,accounts,_=scenario(product)
        summary,_,_,values,_=k.product_tables(product,selection,accounts)
        summaries.extend(summary);daily[product]=values
    result,total,annual=k.combined_tables(summaries,daily)
    opening=next(r for r in result if r["method"]==k.METHODS[0] and r["raw_date"]=="2020-12-29")
    assert opening["participant_count"]==0 and opening["normalized_units"] is None
    selected=next(r for r in result if r["method"]==k.METHODS[0] and r["raw_date"]=="2020-12-30")
    assert selected["participant_count"]==5 and "GFEX.SI" not in selected["participants"]
    assert all(r["participant_count"]==0 for r in result if r["method"]=="ma_trend")
    assert next(r for r in result if r["method"]=="buy_hold_control" and r["raw_date"]=="2020-12-30")["participant_count"]==5
    for row in total:
        if row["normalized_units"] is not None:
            assert sum(x["normalized_units"] for x in annual if x["method"]==row["method"] and x["normalized_units"] is not None)==row["normalized_units"]
    assert selected["normalized_units"]==k.half_even(Fraction(selected["sum_product_units"],5))


@pytest.mark.parametrize("path_kind",["root","child","deep_child","ancestor","review"])
def test_output_overlap_before_write(tmp_path,path_kind):
    source=tmp_path/"input";source.mkdir()
    cases=dict(root=source,child=source/"new",deep_child=source/"a"/"b",ancestor=tmp_path,review=tmp_path/"reviews"/"new")
    with pytest.raises(k.Rejected):k.guard_output(cases[path_kind],[source])
    assert list(source.iterdir())==[]
    k.guard_output(tmp_path/"input-sibling",[source])


def test_selection_uses_accepted_identity_and_refuses_selected_first_bar():
    bars,selection,_,_=scenario()
    records=[]
    for row in selection:
        key=row["key"]
        records.append(dict(raw_date=row["raw_date"],selected_contract=key[0] if key else None,selected_instance=key[1] if key else None,
                            selected_key=list(key) if key else None,status="SELECTED" if key else "NO_ELIGIBLE_PREVIOUS_OI"))
    actual=k.selected_rows(records,bars,rb=True)
    assert actual==selection
    first=next(iter(bars.values()))[0]
    records[0].update(selected_contract=first["symbol"],selected_instance=first["contract_instance_id"],selected_key=list(k.identity(first)),status="SELECTED")
    with pytest.raises(k.Rejected,match="previous_close"):k.selected_rows(records,bars,rb=True)


@pytest.mark.parametrize("mutation",["zero_close","negative_close","nonfinite_close","duplicate","wrong_nano","naive","holdout","trading_holdout"])
def test_bar_dates_and_previous_close_integrity(mutation):
    original=bar("SHFE.RB",0,0,"2020-12-30",100)
    declaration=[dict(product="SHFE.RB",raw_symbol=original["symbol"],instance_id=original["contract_instance_id"])]
    records=[dict(symbol=original["symbol"],duration_seconds="86400",datetime_nano=str(original["datetime_nano"]),ts=original["raw_ts"],trading_day=original["raw_date"],close="100")]
    if mutation=="zero_close":records[0]["close"]="0"
    elif mutation=="negative_close":records[0]["close"]="-1"
    elif mutation=="nonfinite_close":records[0]["close"]="Infinity"
    elif mutation=="duplicate":records.append(dict(records[0]))
    elif mutation=="wrong_nano":records[0]["datetime_nano"]="1"
    elif mutation=="naive":records[0]["ts"]="2020-12-30T15:00:00"
    elif mutation=="holdout":records[0].update(ts="2026-09-05T15:00:00+08:00",close="DO_NOT_PARSE")
    else:records[0].update(trading_day="2026-09-05",close="DO_NOT_PARSE")
    with pytest.raises(k.Rejected):k.prepare_bars("SHFE.RB",declaration,records)


def test_display_precision_does_not_change_arithmetic():
    assert k.display(Fraction(2001,2),"money")=="1,000"
    assert k.display(Fraction(-2003,2),"money")=="-1,002"
    assert k.display(Fraction(201,200),"percent")=="1.00"
    assert k.display(Fraction(-203,200),"percent")=="-1.02"
    assert k.finite_text(Fraction(1,10**24))=="0.000000000000000000000001"


def synthetic_sources(tmp_path,monkeypatch):
    """A complete small source chain with the same on-disk formats as history."""
    root=tmp_path/"SYNTHETIC";root.mkdir()
    repo=root/"repo";repo.mkdir()
    source_root=root/"accepted";source_root.mkdir()
    store=k.Sources()
    def save(path,raw):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        return dict(path=str(path),**k.pin(raw))
    def jfile(path,value):return save(path,k.json_bytes(value))
    manifests={"k44_manifest":dict(payloads={}),"b_manifest":dict(payloads={}),"k43_manifest":dict(payloads={})}
    matrix_roots={name:source_root/name for name in manifests}
    def member(name,relative,raw):
        record=save(matrix_roots[name]/relative,raw)
        manifests[name]["payloads"][relative]={x:record[x] for x in ("bytes","sha256")}
    final=dict(final_tool_build="r2",research_generation_build="r1",files={})
    input_binding=dict(products={})
    rb_sources={}
    global_support={}
    for product in k.PRODUCTS:
        bars,selection,accounts,native=scenario(product)
        global_support[product]=(len(bars),sum(map(len,bars.values())))
        declarations=[dict(product=product,raw_symbol=values[0]["symbol"],instance_id=inst) for inst,values in bars.items()]
        input_bars=[dict(symbol=r["symbol"],duration_seconds=86400,datetime_nano=r["datetime_nano"],ts=r["raw_ts"],trading_day=r["raw_date"],close=k.finite_text(r["close"])) for values in bars.values() for r in values]
        bundle_root=source_root/"inputs"/product
        bundle=dict(root=str(bundle_root),manifest=jfile(bundle_root/"manifest.json",{"SYNTHETIC":True}),payloads={})
        for name,raw in {"bars.csv":k.csv_bytes(input_bars),"contracts.json":k.json_bytes(declarations),"availability.json":k.json_bytes({}),"metadata.json":k.json_bytes({})}.items():
            bundle["payloads"][name]=save(bundle_root/name,raw)
        if product=="SHFE.RB":rb_sources.update(input_manifest=bundle["manifest"],input_payloads=bundle["payloads"])
        else:input_binding["products"][product]=dict(status="READY",bundle=bundle)
        rb=product=="SHFE.RB";name="b_manifest" if rb else "k44_manifest"
        prefix="" if rb else "products/"+product+"/"
        index=[]
        for (inst,method),payloads in native.items():
            symbol=bars[inst][0]["symbol"]
            apath=prefix+"accounts/C1000000/BASE/"+symbol+("" if rb else "-synthetic")+"/"+method
            index.append(dict(product=product,capital="1000000",scenario="BASE",method=method,symbol=symbol,contract_instance_id=inst,account_path=apath,observations=len(bars[inst]),terminal_net="5"))
            for filename,raw in payloads.items():member(name,apath+"/"+filename,raw)
        selections=[]
        for row in selection:
            key=row["key"]
            base=dict(raw_date=row["raw_date"],selected_contract=key[0] if key else None,selected_instance=key[1] if key else None,status="SELECTED" if key else "NO_ELIGIBLE_PREVIOUS_OI")
            if rb:base["selected_key"]=list(key) if key else None
            else:base.update(selected_j=key[5] if key else None,selected_datetime_nano=key[2] if key else None,selected_raw_ts=key[3] if key else None)
            selections.append(base)
        if rb:member("k43_manifest","selection.json",k.json_bytes(selections))
        else:
            member(name,prefix+"selection.csv",k.csv_bytes(selections))
            member(name,prefix+"account-index.csv",k.csv_bytes(index))
            mapping=[{key:r[key] for key in ("symbol","contract_instance_id","datetime_nano","original_ts","original_trading_day")}|{"normalized_raw_ts":r["raw_ts"]} for values in bars.values() for r in values]
            member(name,prefix+"timestamp-map.csv",k.csv_bytes(mapping))
        summary,annual,ready,daily,_=k.product_tables(product,selection,accounts)
        old_summary=[dict(method=r["method"],net=r["net_yuan"],covered_observations=r["selected_days"],max_drawdown=r["max_drawdown_yuan"],positive_years=r["positive_years"],covered_years=r["covered_years"]) for r in summary]
        old_annual=[dict(method=r["method"],year=r["year"],net=r["net_yuan"],covered_observations=r["covered_days"]) for r in annual]
        funding=[dict(product=product,capital=capital,scenario="BASE",method=r["method"],view="ALL",n=r["all_contracts"],never_opened_accounts=r["all_contracts_never_opened"],status="OBSERVED_DESCRIPTIVE_ONLY",**{key:r["all_"+key] for key in k.EVENTS}) for capital in k.CAPITALS for r in ready]
        for filename,records in (("dominant-summary.csv",old_summary),("dominant-year.csv",old_annual),("sensitivity-summary.csv",funding)):
            relative="docs/research/v1-daily-baseline-universe/"+product+"/"+filename
            rec=save(source_root/"prior-public"/relative,k.csv_bytes(records))
            save(repo/relative,k.csv_bytes(records))
            final["files"][relative]=dict(source=rec["path"],bytes=rec["bytes"],sha256=rec["sha256"])
        if rb:
            member("k43_manifest","daily-curves.json",k.json_bytes([dict(method=m,raw_date=day,selected_net=k.cell(value["net"]) if value else None) for m,values in daily.items() for day,value in values.items()]))
    prior_report=save(repo/"docs/research/v1-daily-baseline-rb.md",b"SYNTHETIC immutable RB\n")
    rb_config=jfile(source_root/"rb-config.json",dict(base_assumptions=dict(multiplier="10")))
    verdict=jfile(source_root/"PASS.json",dict(verdict="PASS",unconditional=True,conditional=False))
    authorities={"k44_final":final,"k44_verdict":dict(verdict="PASS",unconditional=True,conditional=False),"k44_inputs":input_binding,
                 "k44_sources":dict(sources={"K43_verdict":verdict,"K40B_verdict":verdict,"docs/research/v1-daily-baseline-rb.md":prior_report,"config/research/v1_daily_baseline_rb_b.json":rb_config}),
                 "economics":dict(products={p:dict(multiplier=str(k.MULTIPLIERS[p])) for p in k.PRODUCTS[1:]}),"rb_sources":rb_sources,"k43_freeze":{}}
    locations={}
    for name,value in {**authorities,**manifests}.items():
        p=matrix_roots[name]/"manifest.json" if name in manifests else source_root/(name+".json")
        rec=jfile(p,value);locations[name]=(str(p),rec["sha256"])
    plan=save(source_root/"PLAN.md",b"SYNTHETIC plan fixture\n");locations["plan"]=(plan["path"],plan["sha256"])
    monkeypatch.setattr(k,"AUTHORITIES",locations)
    monkeypatch.setattr(k,"EXPECTED_SUPPORT",global_support)
    for relative,raw in ((k.TOOL,(ROOT/k.TOOL).read_bytes()),(k.CONFIG,k.json_bytes(k.fixed_config())),(k.TEST,b"# SYNTHETIC fixed test source\n")):
        save(repo/relative,raw)
    monkeypatch.setattr(k,"__file__",str(repo/k.TOOL))
    build=dict(schema="k46-build-bindings/v1",plan_sha256=k.PLAN_HASH,source_repo=str(repo),files={relative:k.pin((repo/relative).read_bytes()) for relative in k.BUILD_FILES})
    record=jfile(root/"owner"/"BUILD.json",build)
    return SimpleNamespace(root=root,repo=repo,source_root=source_root,build_path=Path(record["path"]),build_hash=record["sha256"],store=store)


def run_and_capture(env,output):
    context=k.preflight(env.repo,output,env.build_path,env.build_hash)
    k.aggregate(context)
    # A real terminated harmless subprocess supplies the synthetic receipt's
    # process/stream fields. It never claims to have run the historical CLI.
    child=subprocess.Popen([sys.executable,"-B","-c","print('SYNTHETIC receipt process')"],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    stdout,stderr=child.communicate(timeout=30)
    capture=env.root/(output.name+"-capture");capture.mkdir()
    (capture/"stdout").write_bytes(stdout);(capture/"stderr").write_bytes(stderr)
    now=datetime.now(timezone.utc).isoformat()
    command=[sys.executable,"-B",str(env.repo/k.TOOL),"aggregate","--source-repo",str(env.repo),"--output-dir",str(output),
             "--build-bindings",str(env.build_path),"--build-bindings-sha256",env.build_hash]
    receipt=dict(schema="k46-aggregate-terminal/v1",terminated=True,streams_closed=True,timed_out=False,raw_exit=child.returncode,child_pid=child.pid,
                 output_dir=str(output),manifest=k.pin((output/"manifest.json").read_bytes()),build_bindings_sha256=env.build_hash,
                 locations=k.pin((output/"locations.json").read_bytes()),cwd=str(env.repo),
                 started_utc=now,ended_utc=now,command=command,SYNTHETIC=True,
                 stdout=dict(path=str(capture/"stdout"),**k.pin(stdout)),stderr=dict(path=str(capture/"stderr"),**k.pin(stderr)))
    receipt_path=capture/"RECEIPT.json";receipt_path.write_bytes(k.json_bytes(receipt))
    return context,receipt_path


def test_two_complete_synthetic_aggregates_and_seals_are_identical(tmp_path,monkeypatch):
    env=synthetic_sources(tmp_path,monkeypatch)
    original={p:p.read_bytes() for p in env.repo.rglob("*") if p.is_file()}
    outputs=[]
    for name in ("first","second"):
        output=env.root/"runs"/name
        context,receipt_path=run_and_capture(env,output)
        k.seal(context,receipt_path)
        outputs.append({p.relative_to(output).as_posix():p.read_bytes() for p in output.rglob("*") if p.is_file()})
    assert outputs[0]==outputs[1]
    assert all(p.read_bytes()==raw for p,raw in original.items())
    first=env.root/"runs"/"first"
    check=k.loads((first/"publish"/k.PUBLIC/"CHECK.json").read_bytes())
    assert check["build_files"]==k.loads(env.build_path.read_bytes())["files"]
    assert {name:record["rows"] for name,record in check["tables"].items()}|{}=={name:len(k.rows((first/"publish"/k.PUBLIC/name).read_bytes())) for name in k.TABLES}
    assert all(set(record)=={"sha256","file_count"} for record in check["external_manifests"].values())
    assert str(env.root) not in (first/"publish"/k.PUBLIC/"CHECK.json").read_text(encoding="utf-8")
    text=(first/"publish"/k.REPORT).read_text(encoding="utf-8")
    assert text.split("\n| ",1)[0].count("。")==5
    chunks=[];count=0
    for line in text.splitlines():
        if line.startswith("|"):count+=1
        elif count:chunks.append(count-2);count=0
    assert max(chunks)==35 and all(n<=50 for n in chunks)
    assert len(list((first/"publish").rglob("*.csv")))==8


@pytest.mark.parametrize("mutation",["exit","open_stream","unterminated","wrong_manifest","wrong_build","wrong_output","missing_payload","extra_payload","tampered_payload","tampered_source","tampered_build","wrong_tool","wrong_source_repo","wrong_binding_arg","wrong_hash_arg","tampered_locations"])
def test_seal_refuses_partial_or_unbound_outputs(tmp_path,monkeypatch,mutation):
    env=synthetic_sources(tmp_path,monkeypatch);output=env.root/"runs"/"bad"
    context,receipt_path=run_and_capture(env,output)
    receipt=k.loads(receipt_path.read_bytes())
    if mutation=="exit":receipt["raw_exit"]=1
    elif mutation=="open_stream":receipt["streams_closed"]=False
    elif mutation=="unterminated":receipt["terminated"]=False
    elif mutation=="wrong_manifest":receipt["manifest"]["sha256"]="0"*64
    elif mutation=="wrong_build":receipt["build_bindings_sha256"]="0"*64
    elif mutation=="wrong_output":receipt["output_dir"]=str(env.root/"wrong")
    elif mutation=="missing_payload":(output/"report-references.json").rename(output/"missing-preserved.json")
    elif mutation=="extra_payload":(output/"extra").write_text("SYNTHETIC",encoding="utf-8")
    elif mutation=="tampered_payload":(output/"report-references.json").write_bytes(b"[]\n")
    elif mutation=="tampered_source":next(env.source_root.rglob("decisions.csv")).write_bytes(b"altered\n")
    elif mutation=="tampered_build":(env.repo/k.TEST).write_bytes(b"changed\n")
    elif mutation=="wrong_tool":receipt["command"][receipt["command"].index("aggregate")-1]=str(env.repo/"other.py")
    elif mutation=="wrong_source_repo":receipt["command"][receipt["command"].index("--source-repo")+1]=str(env.root)
    elif mutation=="wrong_binding_arg":receipt["command"][receipt["command"].index("--build-bindings")+1]=str(output/"manifest.json")
    elif mutation=="wrong_hash_arg":receipt["command"][receipt["command"].index("--build-bindings-sha256")+1]="0"*64
    elif mutation=="tampered_locations":
        locations=k.loads((output/"locations.json").read_bytes())
        logical=next(iter(locations));replacement=env.root/"same-bytes-relocated.json"
        replacement.write_bytes(Path(locations[logical]).read_bytes())
        locations[logical]=str(replacement)
        (output/"locations.json").write_bytes(k.json_bytes(locations))
    receipt_path.write_bytes(k.json_bytes(receipt))
    with pytest.raises(k.Rejected):k.seal(context,receipt_path)
    assert not (output/"publish").exists()


@pytest.mark.parametrize("mutation",["config","build","source","existing","overlap"])
def test_preflight_rejects_before_output_creation(tmp_path,monkeypatch,mutation):
    env=synthetic_sources(tmp_path,monkeypatch);output=env.root/"runs"/"refuse"
    if mutation=="config":(env.repo/k.CONFIG).write_bytes(b"{}\n")
    elif mutation=="build":(env.repo/k.TOOL).write_bytes(b"# changed\n")
    elif mutation=="source":Path(k.AUTHORITIES["k44_verdict"][0]).write_bytes(b"{}\n")
    elif mutation=="existing":output.mkdir(parents=True)
    else:output=env.source_root/"new"
    with pytest.raises(k.Rejected):k.preflight(env.repo,output,env.build_path,env.build_hash)
    assert not output.exists() or mutation=="existing"


def test_cli_expected_raw_two_is_preserved():
    process=subprocess.run([sys.executable,"-B","-X","utf8",str(ROOT/k.TOOL),"aggregate"],capture_output=True,timeout=30)
    assert process.returncode==2 and b"required" in process.stderr


def test_no_engine_or_network_import():
    import ast
    tree=ast.parse((ROOT/k.TOOL).read_text(encoding="utf-8"))
    imported={node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node,ast.ImportFrom) and node.module}
    imported.update(alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node,ast.Import) for alias in node.names)
    assert not imported & {"ak_trading","numpy","tqsdk","requests","httpx","socket","subprocess","importlib"}
