"""Immutable recent-history export and atomic pointer for the observer bridge."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date,datetime,timedelta,timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

from ak_trading.futures.spec import parse_product_token
from ak_trading.persistence.config import load_database_settings
from ak_trading.persistence.database import create_database_engine
from ak_trading.persistence.futures_research import ExportRequest, export_futures_bundle
from ak_trading.probability.artifacts import load_frozen_input, safe_path

ROOT = Path(__file__).resolve().parents[1]
CST = timezone(timedelta(hours=8))


def refresh_history(*, engine, symbols, through, pointer, output_root, lookback_days=240):
    import re
    if not symbols or any(not re.fullmatch(r'(SHFE|INE|DCE|CZCE|CFFEX|GFEX)\.[A-Za-z]+\d{3,4}', s) for s in symbols):
        raise ValueError('concrete futures history symbols required')
    if not 90 <= lookback_days <= 1000:
        raise ValueError('lookback must be 90..1000 days')
    pointer,output_root=safe_path(Path(pointer)),safe_path(Path(output_root))
    previous=None
    if pointer.exists():
        previous=json.loads(pointer.read_bytes())
        if previous.get('schema_version')!='infiny_history_pointer_v1':raise ValueError('unexpected pointer format')
        if date.fromisoformat(previous['expected_history_date'])>through:raise ValueError('history pointer cannot move backward')
    end=datetime.combine(through+timedelta(days=1),datetime.min.time(),CST)
    products=tuple(sorted({parse_product_token(re.sub(r'\d+$','',s)) for s in symbols},key=str))
    destination=output_root/('history-'+through.strftime('%Y%m%d')+'-'+uuid.uuid4().hex[:10])
    request=ExportRequest(products=products,span_start=end-timedelta(days=lookback_days),span_end=end,as_of=datetime.now(timezone.utc),universe_cache_path=ROOT/'data/downloads/backfill_contract_universe.json',catalog_path=ROOT/'config/china_futures_catalog.json')
    result=export_futures_bundle(engine,request,destination)
    if result.status!='published':raise ValueError('history export did not complete')
    bundle,digest=load_frozen_input(destination)
    coverage=[]
    for symbol in symbols:
        bars=[bar for bar in bundle.bars if bar.symbol==symbol and (bar.trading_day or bar.ts.astimezone(CST).date())<=through]
        latest=max((bar.trading_day or bar.ts.astimezone(CST).date() for bar in bars),default=None)
        coverage.append({'symbol':symbol,'rows':len(bars),'latest':str(latest) if latest else None})
        if latest!=through or len(bars)<60:raise ValueError('history scope is stale or insufficient; prior pointer retained')
    record={'schema_version':'infiny_history_pointer_v1','bundle':str(destination.resolve()),'manifest_sha256':digest,'expected_history_date':through.isoformat(),'symbols':list(symbols),'coverage':coverage,'recorded_at':datetime.now(timezone.utc).isoformat()}
    pointer.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.history-pointer-',dir=pointer.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(record,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
        os.replace(temp,pointer)
    finally:Path(temp).unlink(missing_ok=True)
    return record


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--symbol',action='append',required=True)
    p.add_argument('--through',type=date.fromisoformat,required=True)
    p.add_argument('--pointer',type=Path,default=ROOT/'data/bridge_history/current.json')
    p.add_argument('--output-root',type=Path,default=ROOT/'data/bridge_history/bundles')
    p.add_argument('--lookback-days',type=int,default=240)
    args=p.parse_args(argv);engine=None
    try:
        if args.through>=datetime.now(CST).date():raise ValueError('only completed prior dates allowed')
        settings=load_database_settings(required=True);settings=replace(settings,statement_timeout_ms=30000)
        engine=create_database_engine(settings)
        result=refresh_history(engine=engine,symbols=args.symbol,through=args.through,pointer=args.pointer,output_root=args.output_root,lookback_days=args.lookback_days)
        print(json.dumps({'status':'published','pointer':str(args.pointer),'coverage':result['coverage'],'expected_history_date':result['expected_history_date']},ensure_ascii=False));return 0
    except Exception as exc:
        print(json.dumps({'status':'blocked','error_type':type(exc).__name__}));return 2
    finally:
        if engine is not None:engine.dispose()


if __name__=='__main__':raise SystemExit(main())
