"""Read-only event ingestion, receiver-clock audit, and immutable shadow segments."""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import heapq
import math
import os
from pathlib import Path
from types import MappingProxyType
from typing import AsyncIterator, Literal, Mapping, Protocol, TYPE_CHECKING

from ak_trading.v2 import CONTRACT_VERSION
from ak_trading.v2.contracts import (
    AccountSnapshot, DataQuality, DecisionRecord, ExecutionResult, FeatureSnapshot, Fill, FrozenInput,
    JsonValue, LedgerCheckpoint, MacroItem, MarketBar, MarketForecast, OrderIntent, RegimeAssessment,
    RunPolicy, StrategyProposal, StrategyQualification, StrategySpec,
)
from ak_trading.v2.schemas import (canonical_json_bytes, decode_record, encode_record,
                                    encode_record_file, parse_json_bytes)
from .experiments import ZERO_HASH, ExperimentError, StageIncompleteError, atomic_write, content, digest

if TYPE_CHECKING:
    from ak_trading.v2.probability.models import ForecastModelSet
    from ak_trading.v2.probability.regimes import RegimeReference
    from ak_trading.v2.seasonality import SeasonalityModel
    from ak_trading.v2.decision.payoffs import PathPayoffModel

SECOND = 1_000_000_000
CHECKS = tuple("clock_verified identity_verified bar_completion_verified trading_day_verified sequence_watermark_verified dedup_verified reorder_verified disconnect_verified late_fill_verified restart_verified exit_timer_verified macro_nonblocking_verified no_order_interface_verified cohort_matured all_groups_passed".split())
CLOCK_FIELDS = frozenset("schema_version clock_id source_kind observed_at_nano utc_offset_nano reference_source raw_evidence raw_evidence_sha256".split())
ENTITY_TYPES = {"features":FeatureSnapshot,"forecasts":MarketForecast,"regimes":RegimeAssessment,
                "proposals":StrategyProposal,"decisions":DecisionRecord,"intents":OrderIntent,
                "execution_results":ExecutionResult,"fills":Fill,"equity":AccountSnapshot,"checkpoints":LedgerCheckpoint}


@dataclass(frozen=True)
class ModelActivationRequest:
    previous_checkpoint_hash: str
    requested_at_nano: int
    experiment_sha256: str
    old_model_hashes: Mapping[str,str]
    new_model_hashes: Mapping[str,str]
    new_qualification_hashes: Mapping[str,str]
    source_hashes: Mapping[str,str]

    def __post_init__(self) -> None:
        from .experiments import require_hash
        require_hash(self.previous_checkpoint_hash);require_hash(self.experiment_sha256)
        if type(self.requested_at_nano) is not int:raise ValueError("activation_time_must_be_integer")
        for name in ("old_model_hashes","new_model_hashes","new_qualification_hashes","source_hashes"):
            values=dict(getattr(self,name))
            for key,h in values.items():
                if not isinstance(key,str) or not key:raise ValueError("activation_source_name_required")
                require_hash(h)
            object.__setattr__(self,name,MappingProxyType(values))
        if not self.new_qualification_hashes:raise ValueError("activation_requires_new_qualifications")


@dataclass(frozen=True)
class FeedEvent:
    event_id: str
    sequence: int
    kind: Literal["BAR", "TIMER", "DISCONNECT", "RECONNECT", "MACRO_UPDATE", "MODEL_ACTIVATION"]
    event_nano: int
    received_at_nano: int
    bar: MarketBar | None
    macro_items: tuple[MacroItem, ...]
    source: str

    def __post_init__(self) -> None:
        if not self.event_id or not self.source or self.kind not in ("BAR","TIMER","DISCONNECT","RECONNECT","MACRO_UPDATE","MODEL_ACTIVATION"):
            raise ValueError("invalid_feed_identity")
        if any(type(v) is not int for v in (self.sequence,self.event_nano,self.received_at_nano)) or self.sequence < 0:
            raise ValueError("invalid_feed_clock_or_sequence")
        if (self.kind == "BAR") != isinstance(self.bar,MarketBar):
            raise ValueError("BAR_requires_bar_other_events_forbid_bar")
        if not isinstance(self.macro_items,tuple) or any(not isinstance(i,MacroItem) for i in self.macro_items):
            raise ValueError("invalid_macro_items")
        if self.kind != "MACRO_UPDATE" and self.macro_items:
            raise ValueError("macro_items_only_in_MACRO_UPDATE")


class ReceiverClock(Protocol):
    def now_nano(self) -> int: ...
    def evidence(self) -> Mapping[str, JsonValue]: ...


class MinuteFeed(Protocol):
    def __aiter__(self) -> AsyncIterator[FeedEvent]: ...


class ReplayClock:
    """Explicitly synthetic clock, incapable of providing real runtime acceptance."""
    def __init__(self, start_nano: int):
        self.current = start_nano

    def now_nano(self) -> int:
        return self.current

    def advance(self, moment: int) -> None:
        if moment < self.current:
            raise ValueError("receiver_clock_reversed")
        self.current = moment

    def evidence(self) -> Mapping[str, JsonValue]:
        raw = "SYNTHETIC offline fixture clock; no UTC offset measurement"
        return {"schema_version":"ak_probability_clock_evidence_v2.1.0","clock_id":"M5_REPLAY",
                "source_kind":"SYNTHETIC","observed_at_nano":self.current,"utc_offset_nano":None,
                "reference_source":"frozen-fixture","raw_evidence":raw,
                "raw_evidence_sha256":hashlib.sha256(raw.encode()).hexdigest()}


class JsonlReplayFeed:
    def __init__(self, path: Path, clock: ReplayClock):
        self.path, self.clock = path, clock

    async def __aiter__(self) -> AsyncIterator[FeedEvent]:
        for line in self.path.read_bytes().splitlines():
            payload = parse_json_bytes(line)
            if set(payload) != {"event_id","sequence","kind","event_nano","received_at_nano","bar","macro_items","source"}:
                raise ValueError("feed_schema_mismatch")
            event = FeedEvent(**{**payload,"bar":decode_record(MarketBar,payload["bar"]) if payload["bar"] else None,
                                 "macro_items":tuple(decode_record(MacroItem,x) for x in payload["macro_items"])})
            self.clock.advance(event.received_at_nano)
            yield event
            await asyncio.sleep(0)


def validate_clock_evidence(evidence: Mapping[str, JsonValue], received: int) -> tuple[str,bool]:
    if set(evidence) != CLOCK_FIELDS or evidence["schema_version"] != "ak_probability_clock_evidence_v2.1.0":
        raise ValueError("clock_evidence_schema_mismatch")
    if evidence["source_kind"] not in ("OS_VERIFIED","SYNTHETIC") or type(evidence["observed_at_nano"]) is not int:
        raise ValueError("clock_evidence_identity_mismatch")
    raw = evidence["raw_evidence"]
    if not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest() != evidence["raw_evidence_sha256"]:
        raise ValueError("clock_raw_evidence_hash_mismatch")
    offset = evidence["utc_offset_nano"]
    observed = evidence["observed_at_nano"]
    same_day = datetime.fromtimestamp(observed//SECOND,timezone.utc).date() == datetime.fromtimestamp(received//SECOND,timezone.utc).date()
    verified = (evidence["source_kind"] == "OS_VERIFIED" and type(offset) is int and abs(offset) <= 100_000_000
                and observed <= received and same_day and bool(raw.strip()) and bool(evidence["reference_source"]))
    return digest(evidence),verified


class PublicationUnavailable(ValueError):
    """A registered request failed at the producer boundary; no substitute forecast."""


@dataclass(frozen=True)
class ReceivedEvent:
    event: FeedEvent
    runner_received_at_nano: int
    validated_at_nano: int
    clock_evidence_sha256: str
    clock_verified: bool
    reasons: tuple[str,...]
    visible_bar: MarketBar | None


class FeedMonitor:
    """Audit original input, buffer unpublished disorder, and prohibit stale additions."""
    def __init__(self, bundle: FrozenInput):
        self.bundle = bundle
        self.seen: dict[str,str] = {}
        self.sequences: dict[int,str] = {}
        self.bar_keys: dict[tuple,str] = {}
        self.watermark = -1
        self.last_receive = -1
        self.last_validated = -1
        self.connected = True
        self.last_bar_end: dict[str,int] = {}
        self.buffer = []
        self.serial = 0
        self.audit = Counter()
        self.clock_evidence: dict[str,dict] = {}
        self.publications: dict[str,bool] = {}
        self.expected_requests: dict[str,tuple[int,int]] = {}
        self.received: list[ReceivedEvent] = []
        self.pending_remaining_lots: dict[str,int] = {}
        self.last_account_nano = -1
        self.published_forecasts: dict[str,MarketForecast] = {}
        self.request_payloads: dict[str,str] = {}
        self.invalid_qualifications: dict[str,str] = {}
        self.attempted_snapshot_ranges: set[tuple[str,str|None]] = set()
        self.execution_queue: dict[tuple[str,str],tuple[MarketBar,str]] = {}
        # Per-strategy account clock at the moment an intent was queued; the
        # next bar must open strictly after this committed boundary.
        self.trigger_account_nano: dict[tuple[str,str],int] = {}
        self.last_lock_hash: str = "0"*64
        self.last_registry_hash: str = "0"*64

    def receive(self,event:FeedEvent,clock:ReceiverClock) -> ReceivedEvent:
        if event.kind == "MODEL_ACTIVATION":raise ValueError("external_feed_cannot_inject_activation")
        received = clock.now_nano()
        if type(received) is not int or received < max(self.last_receive,self.last_validated):
            raise ValueError("receiver_clock_reversed")
        evidence = dict(clock.evidence())
        evidence_hash,verified = validate_clock_evidence(evidence,received)
        self.clock_evidence[evidence_hash] = evidence
        reasons = []
        raw_hash = digest(event)
        if event.event_id in self.seen:
            if self.seen[event.event_id] != raw_hash:
                raise ValueError("conflicting_duplicate_event")
            reasons.append("DUPLICATE")
        elif event.sequence in self.sequences:
            raise ValueError("conflicting_sequence_identity")
        else:
            if event.sequence <= self.watermark:
                reasons.append("OUT_OF_ORDER")
            elif self.watermark >= 0 and event.sequence > self.watermark+1:
                reasons.append("SEQUENCE_GAP")
            self.seen[event.event_id] = raw_hash
            self.sequences[event.sequence] = event.event_id
            self.watermark = max(self.watermark,event.sequence)
        bar = event.bar
        if bar is not None:
            identity = next((i for i in self.bundle.instruments if i.contract_id == bar.contract_id),None)
            session = next((s for s in self.bundle.sessions if s.product == bar.product and s.session_id == bar.session_id),None)
            if identity is None or (identity.raw_symbol,identity.product) != (bar.raw_symbol,bar.product):
                reasons.append("IDENTITY_INVALID")
            if session is None or session.trading_day != bar.trading_day or not session.start_nano <= bar.start_nano < bar.end_nano <= session.end_nano:
                reasons.append("TRADING_DAY_INVALID")
            if bar.duration_seconds != 60 or bar.end_nano-bar.start_nano != 60*SECOND or not bar.complete or bar.actual_minutes != 1 or bar.expected_minutes != 1 or bar.end_nano > received:
                reasons.append("BAR_INCOMPLETE")
            if any(v is None for v in (bar.open,bar.high,bar.low,bar.close)) or bar.quality.status in ("INVALID","MISSING"):
                reasons.append("DATA_INVALID")
            key = bar.contract_id,bar.start_nano,bar.duration_seconds
            existing = self.bar_keys.get(key)
            if existing is not None:
                if existing != digest(bar):
                    reasons.append("CONFLICTING_BAR_REVISION")
                else:
                    reasons.append("DUPLICATE_BAR")
            if received-bar.end_nano > 2*SECOND:
                reasons.append("LATE_FILL")
            if received-bar.end_nano > 120*SECOND:
                reasons.append("DATA_STALE")
            if not any(r in reasons for r in ("IDENTITY_INVALID","TRADING_DAY_INVALID","BAR_INCOMPLETE","DATA_INVALID","CONFLICTING_BAR_REVISION","DUPLICATE","DUPLICATE_BAR")):
                self.bar_keys[key] = digest(bar)
            else:
                bar = None
        validated = clock.now_nano()
        if type(validated) is not int or validated < received:
            raise ValueError("validation_clock_reversed")
        if bar is not None:
            available = max(validated,bar.available_at_nano or validated)
            bar = replace(bar,available_at_nano=available,
                          quality=replace(bar.quality,status="STALE" if "DATA_STALE" in reasons else bar.quality.status,
                                          input_hash=digest([content(bar),received,validated])))
        if event.kind == "DISCONNECT":
            self.connected = False
            reasons.append("DISCONNECTED")
        elif event.kind == "RECONNECT":
            self.connected = True
            reasons.append("RECONNECTED_AWAIT_FRESH_BAR")
        self.last_receive,self.last_validated = received,validated
        self.audit.update(reasons)
        row = ReceivedEvent(event,received,validated,evidence_hash,verified,tuple(reasons),bar)
        self.received.append(row)
        return row

    def buffered(self,row:ReceivedEvent,force=False) -> tuple[ReceivedEvent,...]:
        self.serial += 1
        heapq.heappush(self.buffer,(row.event.event_nano,self.serial,row))
        cutoff = row.runner_received_at_nano-2*SECOND
        ready = []
        while self.buffer and (force or self.buffer[0][0] <= cutoff or row.event.kind in ("DISCONNECT","TIMER")):
            ready.append(heapq.heappop(self.buffer)[2])
        return tuple(ready)

    def can_add_risk(self,contract:str,now:int) -> bool:
        return self.connected and contract in self.last_bar_end and 0 <= now-self.last_bar_end[contract] <= 120*SECOND

    def register_request(self,request_id:str,start:int,end:int) -> None:
        previous = self.expected_requests.setdefault(request_id,(start,end))
        if previous != (start,end):
            raise ValueError("request_window_changed")

    def publish(self,forecast:MarketForecast) -> None:
        original = self.expected_requests.get(forecast.request_id)
        if original != (forecast.window_start_nano,forecast.window_end_nano):
            raise ValueError("unregistered_or_changed_forecast_window")
        success = forecast.published_at_nano < original[0] and forecast.quality.status != "INVALID"
        # First publication, including a failure, fixes the request's outcome.
        self.publications.setdefault(forecast.request_id,success)

    def publication_counts(self) -> tuple[int,int]:
        return sum(self.publications.values()),len(self.expected_requests)


class ShadowJournal:
    """Write entities and checkpoint first, commit event last; old segments stay read-only."""
    def __init__(self,output:Path,initial:LedgerCheckpoint,run_policy:RunPolicy,previous_hash:str=ZERO_HASH):
        output.mkdir(parents=True,exist_ok=False)
        self.output = output
        self.previous_hash = previous_hash
        self.event_sequence = 0
        self.chains = {name:(0,ZERO_HASH) for name in ENTITY_TYPES}
        for name in (*ENTITY_TYPES,"shadow_events"):
            with (output/(name+".jsonl")).open("xb") as handle:
                handle.flush();os.fsync(handle.fileno())
        atomic_write(output/"run_policy.json",encode_record_file("RunPolicy","ak_probability_run_policy_v2.1.0",(run_policy,)))
        self.append_records("checkpoints","SEGMENT_START",(initial,))

    def append_records(self,name:str,event_id:str,records:tuple) -> None:
        if name not in ENTITY_TYPES:
            raise ValueError("unknown_shadow_entity")
        for record in records:
            if not isinstance(record,ENTITY_TYPES[name]):
                raise TypeError("shadow_record_type_mismatch")
            sequence,previous = self.chains[name]
            from ak_trading.v2.schemas import SHADOW_CHECKPOINT_SCHEMA_ID
            row={"schema_version":SHADOW_CHECKPOINT_SCHEMA_ID if name == "checkpoints" else f"ak_probability_shadow_{name}_v2.1.0","contract_version":CONTRACT_VERSION,
                 "sequence":sequence+1,"event_id":event_id,"record":encode_record(record),"previous_hash":previous}
            row["row_hash"]=digest(row)
            with (self.output/(name+".jsonl")).open("ab") as handle:
                handle.write(canonical_json_bytes(row));handle.flush();os.fsync(handle.fileno())
            self.chains[name]=(sequence+1,row["row_hash"])

    def commit(self,row:ReceivedEvent,checkpoint:LedgerCheckpoint,records:Mapping[str,tuple]) -> None:
        published_requests=set()
        for f in records.get("forecasts",()):
            if f.request_id in published_requests:raise ValueError("duplicate_forecast_request_in_event")
            published_requests.add(f.request_id)
        _records={name:list(values) for name,values in records.items()}
        # Bind every known registered request, including a producer failure, to
        # immutable input facts without synthesizing a MarketForecast.
        for name in ENTITY_TYPES:
            if name != "checkpoints":
                self.append_records(name,row.event.event_id,tuple(_records.get(name,())))
        self.append_records("checkpoints",row.event.event_id,(checkpoint,))
        value={"schema_version":"ak_probability_shadow_event_v2.1.3","sequence":self.event_sequence+1,
               "event":content(row.event),"runner_received_at_nano":row.runner_received_at_nano,
               "validated_at_nano":row.validated_at_nano,"clock_evidence_sha256":row.clock_evidence_sha256,
               "decision_ids":[d.decision_id for d in records.get("decisions",())],"checkpoint_hash":checkpoint.checkpoint_hash,
               "previous_hash":self.previous_hash}
        value["row_hash"]=digest(value)
        with (self.output/"shadow_events.jsonl").open("ab") as handle:
            handle.write(canonical_json_bytes(value));handle.flush();os.fsync(handle.fileno())
        self.event_sequence += 1
        self.previous_hash=value["row_hash"]


def read_segment_chain(path:Path,previous:str=ZERO_HASH,allow_tail=False) -> tuple[list[dict],bytes]:
    raw=path.read_bytes()
    lines=raw.splitlines(keepends=True)
    rows=[];ignored=b""
    for index,line in enumerate(lines):
        if not line.endswith(b"\n"):
            if allow_tail and index == len(lines)-1:
                ignored=line;break
            raise ValueError("incomplete_segment_line")
        row=parse_json_bytes(line)
        if row.get("sequence") != len(rows)+1 or row.get("previous_hash") != previous or row.get("row_hash") != digest({k:v for k,v in row.items() if k != "row_hash"}):
            raise ValueError("segment_chain_corrupt")
        rows.append(row);previous=row["row_hash"]
    return rows,ignored


def read_committed_entities(path:Path,committed:set[str]) -> tuple[list[dict],bytes]:
    """After the last committed reference, every byte is an ignored crash tail."""
    raw=path.read_bytes();lines=raw.splitlines(keepends=True)
    last=-1
    for index,line in enumerate(lines):
        if not line.endswith(b"\n"):continue
        try:row=parse_json_bytes(line)
        except (ValueError,TypeError):continue
        if row.get("event_id") in committed or row.get("event_id")=="SEGMENT_START":last=index
    previous=ZERO_HASH;rows=[]
    for line in lines[:last+1]:
        row=parse_json_bytes(line)
        if row.get("sequence")!=len(rows)+1 or row.get("previous_hash")!=previous or row.get("row_hash")!=digest({k:v for k,v in row.items() if k!="row_hash"}):
            raise ValueError("committed_entity_chain_corrupt")
        if row["event_id"] not in committed and row["event_id"]!="SEGMENT_START":raise ValueError("uncommitted_hole_before_committed_entity")
        rows.append(row);previous=row["row_hash"]
    return rows,b"".join(lines[last+1:])


def recover_segment(segment:Path,previous_hash:str=ZERO_HASH) -> tuple[LedgerCheckpoint,str,str|None]:
    events,tail=read_segment_chain(segment/"shadow_events.jsonl",previous_hash,True)
    committed={e["event"]["event_id"] for e in events}
    checks,check_tail=read_committed_entities(segment/"checkpoints.jsonl",committed)
    if not checks or checks[0]["event_id"] != "SEGMENT_START":
        raise ValueError("recovery_initial_checkpoint_missing")
    target=events[-1]["checkpoint_hash"] if events else checks[0]["record"]["checkpoint_hash"]
    matched=[r for r in checks if r["record"]["checkpoint_hash"] == target and (r["event_id"] in committed or r["event_id"] == "SEGMENT_START")]
    if not matched:
        raise ValueError("recovery_checkpoint_missing")
    ignored=[tail,check_tail]
    for name in ENTITY_TYPES:
        rows,partial=read_committed_entities(segment/(name+".jsonl"),committed)
        ignored.append(partial)
        for r in rows:
            if r["event_id"] not in committed and r["event_id"] != "SEGMENT_START":
                ignored.append(canonical_json_bytes(r))
                continue
            decode_record(ENTITY_TYPES[name],r["record"])
    payload=b"".join(ignored)
    return decode_record(LedgerCheckpoint,matched[-1]["record"]),events[-1]["row_hash"] if events else previous_hash,hashlib.sha256(payload).hexdigest() if payload else None


def verify_committed_entities(checkpoint:LedgerCheckpoint,segments:tuple[Path,...],bundle:FrozenInput) -> None:
    """Validate originals in committed prefixes, including closed ancestral fills."""
    entities={name:{} for name in ENTITY_TYPES}
    bars={digest(b):b for b in bundle.bars}
    executions=[];previous=ZERO_HASH
    identities={"features":"snapshot_id","forecasts":"forecast_id","regimes":"assessment_id","proposals":"proposal_id",
                "decisions":"decision_id","intents":"intent_id","fills":"fill_id","checkpoints":"checkpoint_hash"}
    for segment in segments:
        events,_=read_segment_chain(segment/"shadow_events.jsonl",previous,True)
        committed={e["event"]["event_id"] for e in events}
        for e in events:
            if e["event"]["kind"]=="MODEL_ACTIVATION":
                activation_path=segment/"model_activations.json"
                activation_document=parse_json_bytes(activation_path.read_bytes())
                activation_record=next((r for r in activation_document["records"] if "MODEL_ACTIVATION:"+digest(r)==e["event"]["event_id"]),None)
                if activation_record is None:raise ValueError("committed_activation_record_missing")
                for name,h in activation_record["source_hashes"].items():
                    path=segment/"activation_sources"/(name+".json")
                    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=h:raise ValueError("activation_original_missing_or_changed")
            raw=e["event"]["bar"]
            if raw is not None:
                b=decode_record(MarketBar,raw)
                controlled=replace(b,available_at_nano=max(e["validated_at_nano"],b.available_at_nano or e["validated_at_nano"]),
                    quality=replace(b.quality,status="STALE" if e["runner_received_at_nano"]-b.end_nano>120*SECOND else b.quality.status,
                                    input_hash=digest([content(b),e["runner_received_at_nano"],e["validated_at_nano"]])))
                bars[digest(controlled)]=controlled
            clock_path=segment/"clock_evidence"/(e["clock_evidence_sha256"]+".json")
            if not clock_path.is_file() or hashlib.sha256(clock_path.read_bytes()).hexdigest()!=e["clock_evidence_sha256"]:
                raise ValueError("committed_clock_evidence_missing_or_changed")
        for name in ENTITY_TYPES:
            rows,_=read_committed_entities(segment/(name+".jsonl"),committed)
            for row in rows:
                if row["event_id"] not in committed:continue
                value=decode_record(ENTITY_TYPES[name],row["record"])
                if name=="execution_results":executions.append(value)
                if name in identities:
                    key=getattr(value,identities[name])
                    if key in entities[name] and entities[name][key]!=value:raise ValueError("conflicting_committed_entity")
                    entities[name][key]=value
        if events:previous=events[-1]["row_hash"]
    fills=entities["fills"]
    if {k:digest(v) for k,v in fills.items()} != dict(checkpoint.processed_fill_hashes):
        raise ValueError("processed_fill_original_set_mismatch")
    rules={r.rule_id:r for r in bundle.rules}
    for rule_id,h in checkpoint.rule_hashes.items():
        if rule_id not in rules or digest(rules[rule_id])!=h:raise ValueError("consumed_rule_original_missing_or_changed")
    for fill in fills.values():
        if fill.intent_id not in entities["intents"] or not any(fill in e.fills for e in executions):
            raise ValueError("fill_intent_or_receipt_original_missing")
        rule=rules.get(fill.rule_id)
        if rule is None or rule.contract_id!=fill.contract_id or not rule.available_at_nano<=fill.execution_nano or not rule.effective_from_nano<=fill.execution_nano<rule.effective_until_nano:
            raise ValueError("fill_rule_original_invalid")
    for intent in entities["intents"].values():
        if intent.decision_id not in entities["decisions"]:raise ValueError("intent_decision_original_missing")
    for decision in entities["decisions"].values():
        for proposal in decision.proposals:
            if entities["proposals"].get(proposal.proposal_id)!=proposal:raise ValueError("decision_proposal_original_missing")
    for proposal in entities["proposals"].values():
        if proposal.forecast_id is not None and proposal.forecast_id not in entities["forecasts"]:raise ValueError("proposal_forecast_original_missing")
    for forecast in entities["forecasts"].values():
        if forecast.snapshot_id not in entities["features"]:raise ValueError("forecast_features_original_missing")
    for mark in checkpoint.valuation_marks:
        if mark.status=="UNVALUED":continue
        bar=bars.get(mark.source_bar_sha256)
        if bar is None or bar.bar_id!=mark.source_bar_id or bar.contract_id!=mark.contract_id or bar.close!=mark.price or bar.end_nano!=mark.as_of_nano:
            raise ValueError("valuation_bar_original_missing_or_changed")
        if bar.available_at_nano is None or not bar.end_nano<=bar.available_at_nano<=mark.valued_at_nano:
            raise ValueError("valuation_bar_not_visible")


def _segment_ancestors(segment:Path,search_root:Path) -> tuple[Path,...]:
    manifest_path=segment/"manifest.json"
    if not manifest_path.is_file():return (segment,)
    manifest=parse_json_bytes(manifest_path.read_bytes())
    previous=manifest["provenance"]["previous_segment_manifest_sha256"]
    if previous is None:return (segment,)
    matches=[p for p in search_root.rglob("manifest.json") if p!=manifest_path and hashlib.sha256(p.read_bytes()).hexdigest()==previous]
    if len(matches)!=1:raise ValueError("ancestor_segment_manifest_missing_or_ambiguous")
    parent=matches[0].parent
    if parent==segment:raise ValueError("segment_ancestor_cycle")
    chain=_segment_ancestors(parent,search_root)
    if segment in chain:raise ValueError("segment_ancestor_cycle")
    return (*chain,segment)


def verify_activation_request(request:ModelActivationRequest,checkpoint:LedgerCheckpoint,
                              models:Mapping[str,str],qualifications:tuple[StrategyQualification,...],
                              evidence:Mapping[str,bytes],effective_at:int,until_nano:int) -> dict:
    if request.previous_checkpoint_hash!=checkpoint.checkpoint_hash or dict(request.old_model_hashes)!=dict(checkpoint.model_hashes):
        raise ValueError("activation_previous_dependency_mismatch")
    if dict(request.new_model_hashes)!=dict(models):raise ValueError("activation_new_model_mismatch")
    if not max(request.requested_at_nano,checkpoint.account.as_of_nano)<=effective_at<until_nano:
        raise ValueError("activation_time_outside_window")
    if checkpoint.account.lots or checkpoint.pending_intents or checkpoint.pending_remaining_lots or checkpoint.account.reserved_margin_cny:
        raise ValueError("activation_requires_flat_unreserved_account")
    if any(s.episode_id is not None or s.budget is not None or s.pending_intent_ids or any(s.layer_filled_lots) for s in checkpoint.states):
        raise ValueError("activation_requires_all_episodes_ended")
    if checkpoint.account.quality.status!="READY":raise ValueError("activation_account_not_reconciled")
    for h,raw in evidence.items():
        if hashlib.sha256(raw).hexdigest()!=h:raise ValueError("activation_evidence_hash_mismatch")
    if request.experiment_sha256 not in evidence:raise ValueError("activation_experiment_original_missing")
    experiment=parse_json_bytes(evidence[request.experiment_sha256])
    if experiment.get("schema_version")!="ak_probability_experiment_v2.1.0":raise ValueError("activation_experiment_schema_mismatch")
    spec=experiment["spec"]
    if (spec["contract_lock_sha256"]!=checkpoint.contract_lock_sha256 or
        spec["research_policy_version"]!=checkpoint.run_policy.research_policy_version or
        spec["macro_policy_version"]!=checkpoint.run_policy.macro_policy_version):
        raise ValueError("activation_registered_scope_mismatch")
    for name,h in request.source_hashes.items():
        if h not in evidence:raise ValueError("activation_source_original_missing:"+name)
    required_roles={"experiment","old_forecast","old_payoff","old_regime_reference","new_forecast","new_payoff","new_regime_reference","registry","contract_lock"}
    if not required_roles<=set(request.source_hashes):raise ValueError("activation_source_roles_incomplete")
    if request.source_hashes["experiment"]!=request.experiment_sha256 or request.source_hashes["contract_lock"]!=checkpoint.contract_lock_sha256:
        raise ValueError("activation_source_identity_mismatch")
    actual={q.qualification_id:digest(q) for q in qualifications if q.statistics=="PASS" and dict(q.model_hashes)==dict(models)
            and q.issued_at_nano<=effective_at<q.valid_until_nano}
    if dict(request.new_qualification_hashes)!=actual:raise ValueError("activation_new_qualifications_not_passing")
    for q in qualifications:
        if q.qualification_id in actual:
            if not {q.report_sha256,q.review_sha256,q.experiment_sha256}<=set(request.source_hashes.values()):
                raise ValueError("activation_qualification_source_incomplete")
    # All unchanged recovery facts are verified by M4's public restore. This
    # host additionally checks the originals and strict request source mapping.
    if request.source_hashes["registry"]!=checkpoint.registry_sha256:raise ValueError("activation_registry_source_mismatch")
    if any(q.experiment_sha256!=request.experiment_sha256 for q in qualifications if q.qualification_id in request.new_qualification_hashes):
        raise ValueError("activation_qualification_experiment_mismatch")
    return {**content(request),"effective_at_nano":effective_at,"old_qualification_hashes":dict(checkpoint.qualification_hashes)}


def group_acceptance(product:str,kind:str,expected_days:set,expected_bars:int,expected_forecasts:int,
                     observed_days:set,latencies:list[float],complete_bars:int,on_time:int,clock_verified:bool,
                     minimum_days:int=20) -> dict:
    p99=sorted(latencies)[math.ceil(.99*len(latencies))-1] if latencies else 0.0
    ratio=complete_bars/expected_bars if expected_bars else 0.0
    publication=on_time/expected_forecasts if expected_forecasts else 0.0
    reasons=[]
    if len(expected_days)<minimum_days or not expected_days <= observed_days:reasons.append("TRADING_DAY_COVERAGE_INSUFFICIENT")
    if not latencies:reasons.append("NO_LATENCY_OBSERVATIONS")
    if ratio<.995:reasons.append("BAR_COMPLETENESS_BELOW_THRESHOLD")
    if latencies and p99>5:reasons.append("LATENCY_P99_EXCEEDED")
    if publication<.995:reasons.append("PUBLICATION_RATE_BELOW_THRESHOLD")
    if not clock_verified:reasons.append("CLOCK_UNVERIFIED")
    return {"product":product,"session_kind":kind,"expected_trading_days":len(expected_days),
            "observed_trading_days":len(expected_days & observed_days),"expected_bars":expected_bars,
            "complete_bars":complete_bars,"complete_bar_ratio":ratio,"p99_latency_seconds":p99,
            "on_time_forecasts":on_time,"expected_forecasts":expected_forecasts,
            "on_time_publication_ratio":publication,"clock_verified":clock_verified,"passed":not reasons,"reasons":reasons}


def _process_event(row, bundle, ledger, states, pending, forecast_models, payoff_model,
                   seasonality, reference, specs, run_policy, policy, qualifications,
                   clock, monitor, cache, models, qualification_hashes, received_history):
    """One event transaction, using only declared M4/M3B interfaces and records."""
    from ak_trading.v2.decision.execution import (simulate_execution, capacity_before, position_for, rule_at)
    from ak_trading.v2.decision.pipeline import build_strategy_context
    from ak_trading.v2.decision.payoffs import estimate_proposal_payoff
    from ak_trading.v2.decision.gates import evaluate_gate
    from ak_trading.v2.decision.risk import enforce_account_risk
    from ak_trading.v2.decision.routing import choose_proposal
    from ak_trading.v2.strategies.registry import strategy_registry_v2, fresh_state
    from ak_trading.v2.strategies.state import apply_execution, commit_observation, commit_selected_proposal
    from ak_trading.v2.decision.replay import apply_receipt
    from ak_trading.v2.probability.forecast import publish_market_forecast
    now = clock.now_nano()
    if now < row.validated_at_nano:raise ValueError("processing_clock_precedes_validation")
    # Completed-bar discipline: the account may only advance to the bar's end.
    # Latency beyond bar end is receiver evidence, not account time, so a fill
    # at the NEXT bar's open (>= this end) stays monotone with committed state.
    if row.visible_bar is not None and row.event.kind == "BAR":
        now = min(now, row.visible_bar.end_nano)
    records = defaultdict(list)
    # Propagate committed pending remainders from the previous event boundary.
    for key,(scheduled_bar,scheduled_intent) in list(monitor.execution_queue.items()):
        if scheduled_intent not in pending or pending[scheduled_intent].expires_at_nano<=now:
            monitor.execution_queue.pop(key,None)
            monitor.trigger_account_nano.pop(key,None)
    remaining = dict(monitor.pending_remaining_lots)
    for intent in pending.values():
        if intent.intent_id not in remaining:remaining[intent.intent_id]=intent.delta_lots
    for key,state in states.items():
        for intent_id in state.pending_intent_ids:
            if intent_id not in remaining and intent_id in pending:remaining[intent_id]=pending[intent_id].delta_lots
    bar = row.visible_bar
    account_before = ledger.snapshot(now)
    # Cancel new risk on any disconnect/stale timer, while retaining risk exits.
    for intent_id,intent in tuple(pending.items()):
        state = states[(intent.contract_id,intent.strategy_id)]
        if bar is not None and "DATA_STALE" not in row.reasons:
            monitor.last_bar_end[bar.contract_id]=max(monitor.last_bar_end.get(bar.contract_id,0),bar.end_nano)
        result = None
        # Execute only against the NEXT completed bar after the intent, never
        # the trigger bar whose open precedes the already-committed clock.
        if bar is not None and ("LATE_FILL" not in row.reasons and "DATA_STALE" not in row.reasons):
            key=(intent.contract_id,intent.strategy_id)
            scheduled=monitor.execution_queue.get(key)
            if scheduled is not None and scheduled[1]==intent_id and scheduled[0].end_nano<bar.end_nano and scheduled[0].bar_id!=bar.bar_id:
                rule = rule_at(bundle.rules,intent.contract_id,bar.start_nano)
                capacity = capacity_before(bundle.bars,intent.contract_id,60,bar.start_nano-1,rule)
                if capacity is None:
                    # Capacity uses bars visible BEFORE this execution bar; the
                    # trigger bar itself already qualifies as history.
                    visible=tuple(b for b in received_history.values() if b.duration_seconds==60 and b.complete and b.available_at_nano is not None and b.end_nano<bar.start_nano)
                    capacity=capacity_before(visible,intent.contract_id,60,bar.start_nano-1,rule)
                if rule is not None and capacity is not None and bar.end_nano>monitor.trigger_account_nano.get((intent.contract_id,intent.strategy_id),0):
                    active = replace(intent,delta_lots=remaining[intent_id])
                    result = simulate_execution(active,bar,rule,capacity,position_for(account_before,intent.contract_id))
                    monitor.execution_queue.pop(key,None)
                    monitor.trigger_account_nano.pop(key,None)
                else:
                    result = ExecutionResult(intent_id,(),remaining[intent_id],"BLOCKED",("CAPACITY_UNKNOWN","EXIT_PENDING"))
        qualification=next((q for q in qualifications if q.strategy_id==intent.strategy_id and intent.contract_id in q.contract_ids
                             and q.issued_at_nano<=now<q.valid_until_nano and q.statistics=="PASS" and dict(q.model_hashes)==dict(models)),None)
        invalid_pending=(intent.offset=="OPEN" and (now>=forecast_models.valid_until_nano or
                         (run_policy.purpose=="SHADOW_OPERATIONAL" and (qualification is None or qualification.qualification_id in monitor.invalid_qualifications))))
        if invalid_pending:
            result=ExecutionResult(intent_id,(),remaining[intent_id],"CANCELLED",("QUALIFICATION_EXPIRED",))
        elif result is None and intent.expires_at_nano <= now:
            result = ExecutionResult(intent_id,(),remaining[intent_id],"EXPIRED",("INTENT_EXPIRED",))
        elif result is None and intent.offset == "OPEN" and not monitor.can_add_risk(intent.contract_id,now):
            result = ExecutionResult(intent_id,(),remaining[intent_id],"CANCELLED",("DATA_STALE",))
        elif result is None and bar is not None and bar.contract_id == intent.contract_id:
            monitor.execution_queue[(intent.contract_id,intent.strategy_id)]=(bar,intent_id)
            monitor.trigger_account_nano[(intent.contract_id,intent.strategy_id)]=now
        if result is None:
            continue
        records["execution_results"].append(result)
        next_state,fresh=apply_receipt(ledger,state,intent,result,remaining[intent_id],bundle.rules,now)
        states[(intent.contract_id,intent.strategy_id)]=next_state
        records["fills"].extend(fresh)
        if fresh:
            ledger.record_event("capacity:"+row.event.event_id+":"+intent_id,
                                {intent.contract_id+"|"+str(bar.start_nano):sum(f.lots for f in fresh)})
        if result.status in ("FILLED","EXPIRED","CANCELLED"):
            pending.pop(intent_id);remaining.pop(intent_id)
        else:
            remaining[intent_id] = result.remaining_lots
    day = bar.trading_day if bar is not None else account_before.trading_day
    if row.event.kind == "TIMER":
        active_sessions = [s for s in bundle.sessions if s.start_nano <= now <= s.end_nano]
        if active_sessions:
            day = max(s.trading_day for s in active_sessions)
    marks = {l.contract_id: (bar if bar is not None and bar.contract_id == l.contract_id else None)
             for l in ledger.snapshot(now).lots}
    records["equity"].append(ledger.mark(marks,now,day,bundle.rules))
    # A timer can create an exit without a fresh forecast or a bar.
    for key,state in tuple(states.items()):
        due = state.active_exit_nano is not None and now >= state.active_exit_nano
        exposed = [l for l in ledger.snapshot(now).lots if (l.contract_id,l.strategy_id)==key]
        if exposed and (due or not monitor.can_add_risk(state.contract_id,now)):
            states[key] = replace(state,phase="EXITING")
            if not any(i.offset != "OPEN" and i.episode_id==state.episode_id for i in pending.values()):
                new_intents=[]
                for offset in ("CLOSE_TODAY","CLOSE"):
                    lots=[l for l in exposed if (l.opened_trading_day==day)==(offset=="CLOSE_TODAY")]
                    if not lots:continue
                    delta=-sum(l.side*l.lots for l in lots)
                    identity=digest([row.event.event_id,state.episode_id,offset])
                    decision_id="decision:"+identity
                    record=DecisionRecord(decision_id,"opportunity:"+identity,now,run_policy.research_policy_version,
                                          digest(run_policy),run_policy.purpose,(),run_policy.routing_mode,(),(),{},run_policy.seed,
                                          None,None,"MANAGE_POSITION",("EXIT_PENDING",),digest(ledger.snapshot(now)))
                    intent=OrderIntent("intent:"+identity,decision_id,state.strategy_id,state.episode_id,state.contract_id,
                                       now,now+1,max(now+1, max(s.end_nano for s in bundle.sessions))+1,
                                       delta,offset,"TIME_EXIT" if due else "STOP",None,None)
                    new_intents.append(intent);records["decisions"].append(record);records["intents"].append(intent)
                    pending[intent.intent_id]=intent;remaining[intent.intent_id]=delta
                states[key]=replace(states[key],pending_intent_ids=states[key].pending_intent_ids+tuple(i.intent_id for i in new_intents))
    if bar is not None and row.event.kind == "BAR":
        # The feed carries minutes. Derive a daily bar only after every planned
        # session of this trading day is complete and actually received.
        from ak_trading.v2.clock import aggregate_bars
        day_sessions=tuple(s for s in bundle.sessions if s.product==bar.product and s.trading_day==bar.trading_day)
        complete_day=bool(day_sessions) and max(s.end_nano for s in day_sessions)<=now
        daily_ready=False
        if complete_day:
            minute_source=tuple(b for b in bundle.bars if b.contract_id==bar.contract_id and b.duration_seconds==60 and b.trading_day==bar.trading_day)
            expected=sum((s.end_nano-s.start_nano)//(60*SECOND) for s in day_sessions)
            if len({b.start_nano for b in minute_source if b.complete and b.available_at_nano is not None and b.available_at_nano<=now})==expected:
                aggregates=aggregate_bars(minute_source,day_sessions,86400,now)
                if len(aggregates)==1 and aggregates[0].complete:
                    daily=aggregates[0]
                    if not any(b.contract_id==daily.contract_id and b.duration_seconds==86400 and b.trading_day==daily.trading_day for b in bundle.bars):
                        bundle=replace(bundle,bars=(*bundle.bars,daily))
                    daily_ready=True
        contexts={};proposals=[]
        for spec in specs:
            if spec.mode=="DAILY" and not daily_ready:continue
            key=(bar.contract_id,spec.strategy_id)
            state=states.get(key)
            actual=[]
            def publish_one(model,request,regime):
                records["features"].append(request.features)
                records["regimes"].append(regime)
                monitor.register_request(request.request_id,request.window_start_nano,request.window_end_nano)
                request_hash=digest(request)
                if request.request_id in monitor.published_forecasts:
                    old=monitor.published_forecasts[request.request_id]
                    if (old.snapshot_id,old.contract_id,old.horizon,old.window_start_nano,old.window_end_nano,old.reference_price,old.atr_points,old.upper_distance,old.lower_distance,old.frozen_range)!=(
                        request.features.snapshot_id,request.features.contract_id,request.horizon,request.window_start_nano,request.window_end_nano,request.features.reference_price,
                        request.features.atr_points,request.upper_distance,request.lower_distance,request.frozen_range):raise ValueError("forecast_request_payload_changed")
                    if request.request_id in monitor.request_payloads and monitor.request_payloads[request.request_id]!=request_hash:raise ValueError("forecast_request_payload_changed")
                    prediction=old
                else:
                    try:
                        prediction=publish_market_forecast(model,request,regime,clock_nano=clock.now_nano)
                    except (ValueError,TypeError) as error:
                        monitor.publications.setdefault(request.request_id,False)
                        monitor.audit["PUBLICATION_FAILED:"+str(error)]+=1
                        raise PublicationUnavailable("PUBLICATION_FAILED:"+request.request_id+":"+str(error)) from error
                    monitor.published_forecasts[request.request_id]=prediction
                    monitor.request_payloads[request.request_id]=request_hash
                monitor.publish(prediction);actual.append(prediction)
                records["forecasts"].append(prediction)
                return prediction
            def provider(model,request,regime):
                from ak_trading.v2.probability.forecast import build_forecast_requests
                from ak_trading.v2.contracts import Horizon
                identity=request.features.snapshot_id,request.frozen_range.range_id if request.frozen_range else None
                if identity not in monitor.attempted_snapshot_ranges:
                    monitor.attempted_snapshot_ranges.add(identity)
                    values=(1,5,20) if request.features.duration_seconds==86400 else (1,5,15,30,60)
                    unit="TRADING_DAY" if request.features.duration_seconds==86400 else "TRADING_MINUTE"
                    from ak_trading.v2.clock import ClockError
                    for value in values:
                        try:batch=build_forecast_requests(request.features,(Horizon(unit,value),),request.frozen_range,bundle.sessions)
                        except ClockError:
                            monitor.audit[f"REQUEST_CALENDAR_MISSING:{unit}:{value}"]+=1
                            continue
                        for other in batch:
                            try:publish_one(model,other,regime)
                            except PublicationUnavailable:pass
                if request.request_id in monitor.published_forecasts:
                    return monitor.published_forecasts[request.request_id]
                if request.request_id in monitor.publications:
                    raise PublicationUnavailable("PUBLICATION_FAILED:"+request.request_id)
                return publish_one(model,request,regime)
            try:
                from ak_trading.v2.strategies.registry import select_exit_binding as _seb4
                context=build_strategy_context(bundle,bar.contract_id,now,ledger.snapshot(now),state,forecast_models,
                                               payoff_model,spec,seasonality,reference,run_policy,qualifications,
                                               forecast_provider=provider,
                                               exit_binding=_seb4(spec))
            except PublicationUnavailable as error:
                identity=digest([row.event.event_id,spec.strategy_id,"PUBLICATION_FAILED",str(error)])
                records["decisions"].append(DecisionRecord("decision:"+identity,"opportunity:"+identity,now,
                    run_policy.research_policy_version,digest(run_policy),run_policy.purpose,(),run_policy.routing_mode,(),(),{"CASH":1.0},
                    run_policy.seed,None,None,"NO_TRADE",("PUBLICATION_FAILED",str(error)),digest(ledger.snapshot(now))))
                continue
            proposal=strategy_registry_v2(spec.mode)[spec.strategy_id].evaluate(context,state)
            opportunity="opportunity:"+digest([bar.contract_id,now,content(spec.max_holding),digest(context.account)])
            if proposal.action in ("OPEN","ADD"):
                proposal=replace(proposal,payoff=estimate_proposal_payoff(payoff_model,proposal,context,opportunity))
            contexts[proposal.proposal_id]=context;proposals.append(proposal)
            states[key]=commit_observation(state,proposal)
            records["features"].append(context.features);records["forecasts"].extend(actual);records["regimes"].append(context.regime)
        # Different horizons are independent opportunity sets, never sorted by unlike EV.
        by_horizon=defaultdict(list)
        for p in proposals:by_horizon[contexts[p.proposal_id].spec.max_holding].append(p)
        for horizon,candidates in sorted(by_horizon.items(),key=lambda item:(item[0].unit,item[0].value)):
            gates=[]
            for p in candidates:
                gate=evaluate_gate(p,contexts[p.proposal_id],policy,checked_at_nano=clock.now_nano())
                if p.action in ("OPEN","ADD") and run_policy.purpose=="SHADOW_OPERATIONAL" and not row.clock_verified:
                    gate=replace(gate,eligible=False,reasons=tuple(sorted(set((*gate.reasons,"CLOCK_UNVERIFIED")))))
                qualification=contexts[p.proposal_id].qualification
                if p.action in ("OPEN","ADD") and qualification is not None and qualification.qualification_id in monitor.invalid_qualifications:
                    gate=replace(gate,eligible=False,reasons=tuple(sorted(set((*gate.reasons,monitor.invalid_qualifications[qualification.qualification_id])))))
                if p.action in ("OPEN","ADD") and not monitor.can_add_risk(p.contract_id,now):
                    gate=replace(gate,eligible=False,reasons=tuple(sorted(set((*gate.reasons,"DATA_STALE")))))
                gates.append(gate)
            account=ledger.snapshot(now)
            capacity={bar.contract_id:capacity_before(bundle.bars,bar.contract_id,60,now,rule_at(bundle.rules,bar.contract_id,now)) or 0}
            # Risk reads a candidate snapshot without mutating the ledger's
            # committed sequence: a detached fork identical to restore semantics.
            from ak_trading.v2.decision.ledger import ContinuousLedger as _Ledger
            risk_account=ledger.snapshot(now)
            risk_marks=tuple(mark for _,mark in sorted(getattr(ledger,"_marks",{}).items(),key=lambda kv:kv[1].contract_id))
            gates=enforce_account_risk(tuple(candidates),tuple(gates),risk_account,bundle.instruments,tuple(states.values()),
                                       tuple(pending.values()),bundle.rules,capacity,policy,
                                       valuations=risk_marks)
            submit_at=clock.now_nano()
            refreshed=[]
            for p,g in zip(candidates,gates):
                current=evaluate_gate(p,contexts[p.proposal_id],policy,checked_at_nano=submit_at)
                refreshed.append(replace(g,eligible=g.eligible and current.eligible,
                                         reasons=tuple(sorted(set((*g.reasons,*current.reasons))))))
            gates=tuple(refreshed)
            opportunity="opportunity:"+digest([bar.contract_id,now,content(horizon),digest(account)])
            qids=tuple(sorted({q.qualification_id for q in qualifications if any(c.spec.strategy_id==q.strategy_id for c in contexts.values())}))
            decision=choose_proposal(tuple(candidates),gates,account,opportunity,now,run_policy,qids)
            records["decisions"].append(decision)
            selected=next((p for p in candidates if p.proposal_id==decision.selected_proposal_id),None)
            if selected is not None and selected.action in ("OPEN","ADD"):
                ctx=contexts[selected.proposal_id]
                held=sum(l.side*l.lots for l in account.lots if l.contract_id==selected.contract_id)
                delta=selected.target_lots-held
                if delta:
                    from ak_trading.v2.decision.replay import intents_for_proposal
                    new=intents_for_proposal(selected,decision.decision_id,account,ctx.rule)
                    upper=min([selected.valid_until_nano,forecast_models.valid_until_nano]
                              +[f.expires_at_nano for f in ctx.forecasts if f.forecast_id==selected.forecast_id]
                              +([ctx.qualification.valid_until_nano] if ctx.qualification else []))
                    new=tuple(replace(i,created_at_nano=submit_at,not_before_nano=max(i.not_before_nano,submit_at+1),expires_at_nano=upper)
                              for i in new if max(i.not_before_nano,submit_at+1)<upper)
                    if new:
                        key=(selected.contract_id,selected.strategy_id)
                        states[key]=commit_selected_proposal(states[key],selected,new)
                        for intent in new:
                            pending[intent.intent_id]=intent;remaining[intent.intent_id]=intent.delta_lots
                        records["intents"].extend(new)
            ledger.sync_reservations(tuple(states.values()),bundle.rules,now)
        records["proposals"].extend(proposals)
    from ak_trading.v2.decision.ledger import ContinuousLedger as _Ledger
    from ak_trading.v2.strategies.registry import select_exit_binding
    _event_bindings=tuple(sorted({select_exit_binding(s).binding_sha256:select_exit_binding(s) for s in specs}.values(),key=lambda b:b.binding_sha256))
    checkpoint=ledger.checkpoint(tuple(states.values()),tuple(pending.values()),models,qualification_hashes,
                                 pending_remaining_lots=remaining,exit_registry_sha256=getattr(monitor,"exit_registry_sha256","0"*64),
                                 exit_bindings=_event_bindings)
    ledger.record_event(row.event.event_id,{})
    monitor.pending_remaining_lots=remaining
    monitor.last_account_nano=now
    monitor.last_lock_hash=checkpoint.contract_lock_sha256
    monitor.last_registry_hash=checkpoint.registry_sha256
    # Deduplicate shared features/forecasts across strategy evaluations before writing entities.
    for name,attribute in (("features","snapshot_id"),("forecasts","forecast_id"),("regimes","assessment_id")):
        unique={}
        for value in records[name]:
            key=getattr(value,attribute)
            if key in unique and unique[key]!=value:raise ValueError("conflicting_shadow_entity")
            unique[key]=value
        records[name]=list(unique.values())
    return checkpoint,{k:tuple(v) for k,v in records.items()}


def _acceptance(bundle,monitor,records,start,end,as_of):
    from ak_trading.v2.clock import horizon_window,ClockError
    from ak_trading.v2.contracts import Horizon
    groups=[];contracts=[]
    # The input calendar fixes every denominator before looking at successes.
    for product in sorted({i.product for i in bundle.instruments}):
        for kind in ("DAY","NIGHT"):
            sessions=[s for s in bundle.sessions if s.product==product and s.kind==kind and start<=s.start_nano<end]
            if not sessions:continue
            expected_days={s.trading_day for s in sessions}
            expected_bars=sum((min(s.end_nano,end)-s.start_nano)//(60*SECOND) for s in sessions)
            rows=[r for r in monitor.received if r.visible_bar is not None and r.visible_bar.product==product
                  and any(s.session_id==r.visible_bar.session_id for s in sessions)]
            on_time=sum(bool(value) for rid,value in monitor.publications.items()
                        if rid in {f.request_id for f in records["forecasts"] if f.product==product and f.session_kind==kind})
            expected_forecasts=expected_bars*5*3
            group=group_acceptance(product,kind,expected_days,expected_bars,expected_forecasts,
                                   {r.visible_bar.trading_day for r in rows},[(r.validated_at_nano-r.visible_bar.end_nano)/SECOND for r in rows],
                                   len({r.visible_bar.bar_id for r in rows}),on_time,bool(rows) and all(r.clock_verified for r in rows))
            groups.append(group)
            for instrument in (i for i in bundle.instruments if i.product==product):
                active=[s for s in sessions if (instrument.listing_nano is None or s.end_nano>instrument.listing_nano)
                        and (instrument.last_trade_nano is None or s.start_nano<instrument.last_trade_nano)]
                if not active:continue
                own=[r for r in rows if r.visible_bar.contract_id==instrument.contract_id]
                g=group_acceptance(product,kind,{s.trading_day for s in active},
                                   sum((min(s.end_nano,end)-s.start_nano)//(60*SECOND) for s in active),
                                   sum((min(s.end_nano,end)-s.start_nano)//(60*SECOND) for s in active)*15,
                                   {r.visible_bar.trading_day for r in own},[(r.validated_at_nano-r.visible_bar.end_nano)/SECOND for r in own],
                                   len({r.visible_bar.bar_id for r in own}),
                                   sum(monitor.publications.get(f.request_id,False) for f in {f.request_id:f for f in records["forecasts"] if f.contract_id==instrument.contract_id and f.session_kind==kind}.values()),
                                   bool(own) and all(r.clock_verified for r in own),minimum_days=len({s.trading_day for s in active}))
                contracts.append({**g,"contract_id":instrument.contract_id,"active_start_nano":min(s.start_nano for s in active),
                                  "active_end_nano":max(s.end_nano for s in active)})
    matured=[]
    for unit,values in (("TRADING_MINUTE",(1,5,15,30,60)),("TRADING_DAY",(1,5,20))):
        for value in values:
            forecasts=[f for f in records["forecasts"] if f.horizon==Horizon(unit,value) and start<=f.decision_nano<end]
            if forecasts and all(f.window_end_nano<=as_of for f in forecasts):matured.append(f"{unit}:{value}")
    checks={key:False for key in CHECKS}
    checks.update(clock_verified=bool(monitor.received) and all(r.clock_verified for r in monitor.received),
                  identity_verified=not monitor.audit["IDENTITY_INVALID"],bar_completion_verified=not monitor.audit["BAR_INCOMPLETE"],
                  trading_day_verified=not monitor.audit["TRADING_DAY_INVALID"],
                  sequence_watermark_verified=not monitor.audit["SEQUENCE_GAP"],
                  dedup_verified=bool(monitor.audit["DUPLICATE"]),reorder_verified=bool(monitor.audit["OUT_OF_ORDER"]),
                  disconnect_verified=bool(monitor.audit["DISCONNECTED"]),late_fill_verified=bool(monitor.audit["LATE_FILL"]),
                  exit_timer_verified=any(r.event.kind=="TIMER" for r in monitor.received),
                  macro_nonblocking_verified=True,no_order_interface_verified=True,
                  cohort_matured=len(matured)==8,all_groups_passed=bool(groups) and all(g["passed"] for g in groups+contracts))
    reasons=[key.upper()+"_NOT_VERIFIED" for key,value in checks.items() if not value]
    if any(e["source_kind"]=="SYNTHETIC" for e in monitor.clock_evidence.values()):reasons.append("SYNTHETIC_REPLAY_NOT_REAL_FEED")
    # External source/UTC evidence and cohort labels require independent verification.
    reasons.append("REAL_FEED_AND_MATURE_LABEL_EVIDENCE_NOT_ACCEPTED")
    return {"schema_version":"ak_probability_shadow_acceptance_v2.1.0","source":",".join(sorted({r.event.source for r in monitor.received})),
            "cohort_start_nano":start,"cohort_end_nano":end,"evaluated_at_nano":as_of,"group_metrics":groups,
            "contract_metrics":contracts,"clock_evidence_hashes":sorted(monitor.clock_evidence),"matured_horizons":matured,
            "checks":checks,"runtime":"SHADOW_PENDING","reasons":reasons}


async def run_shadow(feed: MinuteFeed, bundle: FrozenInput,
                      forecast_models: ForecastModelSet, payoff_model: PathPayoffModel,
                      seasonality_model: SeasonalityModel | None,
                      regime_reference: RegimeReference,
                      specs: tuple[StrategySpec, ...], checkpoint: LedgerCheckpoint | None,
                      run_policy: RunPolicy, policy: Mapping[str, JsonValue],
                      qualifications: tuple[StrategyQualification, ...],
                      qualification_evidence: Mapping[str, bytes],
                      receiver_clock: ReceiverClock,
                      contract_lock: Path,
                      previous_segment_manifest: Path | None,
                      recovery_segment: Path | None,
                      output: Path, until_nano: int, *,
                      activation_request: ModelActivationRequest | None) -> Mapping[str, JsonValue]:
    from ak_trading.v2.artifacts import verify_contract_lock,verify_qualification_evidence
    from ak_trading.v2.decision.ledger import ContinuousLedger
    from ak_trading.v2.decision.pipeline import model_hashes_for,registry_hash_for
    from ak_trading.v2.macro import MacroCache
    if previous_segment_manifest is not None and recovery_segment is not None:
        raise ValueError("ambiguous_recovery_source")
    project=Path(__file__).resolve().parents[4]
    verify_contract_lock(contract_lock,project)
    lock=hashlib.sha256(contract_lock.read_bytes()).hexdigest()
    registry=registry_hash_for(specs)
    models=model_hashes_for(forecast_models,payoff_model,regime_reference,seasonality_model)
    qualifications_by_id={q.qualification_id:q for q in qualifications}
    if len(qualifications_by_id)!=len(qualifications):raise ValueError("duplicate_qualification")
    invalid_qualifications={}
    for q in qualifications:
        spec=next((s for s in specs if s.strategy_id==q.strategy_id),None)
        if spec is None:raise ValueError("unknown_qualified_strategy")
        try:
            verify_qualification_evidence(q,spec,qualification_evidence,lock,registry)
        except (ValueError,TypeError) as error:
            missing=any(h not in qualification_evidence for h in (q.report_sha256,q.review_sha256,q.experiment_sha256))
            invalid_qualifications[q.qualification_id]="QUALIFICATION_EVIDENCE_MISSING" if missing else "QUALIFICATION_EVIDENCE_INVALID"
    previous=ZERO_HASH;previous_manifest_hash=None;recovery_hash=None;ignored=None
    source=None
    if previous_segment_manifest is not None:
        source=previous_segment_manifest.parent
        raw=previous_segment_manifest.read_bytes();manifest=parse_json_bytes(raw)
        for name,h in manifest["files"].items():
            p=(source/name).resolve()
            if not p.is_relative_to(source.resolve()) or hashlib.sha256(p.read_bytes()).hexdigest()!=h:
                raise ValueError("previous_segment_manifest_mismatch")
        previous_manifest_hash=hashlib.sha256(raw).hexdigest()
    elif recovery_segment is not None:
        source=recovery_segment
        if (source/"manifest.json").exists():raise ValueError("recovery_segment_already_published")
        recovery_hash=digest({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(source.glob("*.jsonl"))})
    ancestors=()
    if source is not None:
        ancestors=_segment_ancestors(source,source.parent)
        previous_link=ZERO_HASH
        for ancestor in ancestors[:-1]:
            events,_=read_segment_chain(ancestor/"shadow_events.jsonl",previous_link)
            if events:previous_link=events[-1]["row_hash"]
        restored,previous,ignored=recover_segment(source,previous_link)
        if checkpoint is not None and checkpoint != restored:raise ValueError("explicit_recovery_checkpoint_mismatch")
        checkpoint=restored
        verify_committed_entities(checkpoint,ancestors,bundle)
    if source is not None:
        if activation_request is not None and (source/"activation_request.json").is_file():
            stored=parse_json_bytes((source/"activation_request.json").read_bytes())
            activation_records=parse_json_bytes((source/"model_activations.json").read_bytes())["records"]
            source_events,_=read_segment_chain(source/"shadow_events.jsonl",previous_link,True)
            matching=next((r for r in activation_records if r["previous_checkpoint_hash"]==activation_request.previous_checkpoint_hash),None)
            if matching is not None:
                if stored.get("request")!=content(activation_request):raise ValueError("conflicting_replayed_activation_request")
                if any(e["event"]["event_id"]=="MODEL_ACTIVATION:"+digest(matching) for e in source_events):
                    if dict(checkpoint.model_hashes)!=dict(models):raise ValueError("replayed_activation_new_model_mismatch")
                    activation_request=None
    if checkpoint is None:raise ValueError("verified_initial_checkpoint_required")
    qhashes={key:digest(qualifications_by_id[key]) for key in checkpoint.qualification_hashes if key in qualifications_by_id}
    old_dependencies=checkpoint.model_hashes if activation_request is not None else models
    ledger=ContinuousLedger.restore(checkpoint,bundle.instruments,bundle.rules,run_policy,lock,registry,old_dependencies,qhashes)
    journal=ShadowJournal(output,checkpoint,run_policy,previous)
    if source is None and (checkpoint.processed_fill_hashes or checkpoint.account.lots or checkpoint.pending_intents):
        raise ValueError("first_segment_requires_verified_initial_state")
    if activation_request is not None:
        if source is None:raise ValueError("activation_requires_previous_segment")
        if invalid_qualifications:raise ValueError("activation_qualification_evidence_invalid")
        effective=receiver_clock.now_nano()
        evidence=receiver_clock.evidence();clock_hash,verified=validate_clock_evidence(evidence,effective)
        if not verified:raise ValueError("activation_clock_unverified")
        if not max(forecast_models.calibration_end_nano,payoff_model.training_end_nano,regime_reference.available_at_nano)<effective<forecast_models.valid_until_nano:
            raise ValueError("activation_models_not_available_or_expired")
        record=verify_activation_request(activation_request,checkpoint,models,qualifications,qualification_evidence,effective,until_nano)
        for name,h in activation_request.source_hashes.items():
            if any(part in name for part in ("/","\\","..",":")):raise ValueError("unsafe_activation_source_name")
            atomic_write(output/"activation_sources"/(name+".json"),qualification_evidence[h])
        atomic_write(output/"activation_request.json",canonical_json_bytes({"schema_version":"ak_probability_model_activation_request_v2.1.3","request":content(activation_request)}))
        atomic_write(output/"model_activations.json",canonical_json_bytes({"schema_version":"ak_probability_model_activations_v2.1.3","records":[record]}))
        atomic_write(output/"clock_evidence"/(clock_hash+".json"),canonical_json_bytes(content(evidence)))
        event_id="MODEL_ACTIVATION:"+digest(record)
        states=tuple(replace(s,setup=None,last_regime=None,confirmation_count=0,direction=0,
                             phase="PAUSED" if s.phase=="PAUSED" else "OBSERVE") for s in checkpoint.states)
        qhashes={**qhashes,**dict(activation_request.new_qualification_hashes)}
        ledger.record_event(event_id,{})
        checkpoint=ledger.checkpoint(states,(),models,qhashes,pending_remaining_lots={})
        activation=FeedEvent(event_id,1,"MODEL_ACTIVATION",effective,effective,None,(),"M5:model-activation")
        journal.commit(ReceivedEvent(activation,effective,effective,clock_hash,True,(),None),checkpoint,{})
    else:
        atomic_write(output/"model_activations.json",canonical_json_bytes({"schema_version":"ak_probability_model_activations_v2.1.3","records":[]}))
    monitor=FeedMonitor(bundle)
    monitor.invalid_qualifications=invalid_qualifications
    for ancestor in ancestors:
        # Restore publication outcomes from committed entities, never recompute a
        # previously failed request or shrink the denominator on restart.
        event_rows,_=read_segment_chain(ancestor/"shadow_events.jsonl",
            parse_json_bytes((ancestor/"shadow_events.jsonl").read_bytes().splitlines()[0])["previous_hash"] if (ancestor/"shadow_events.jsonl").stat().st_size else ZERO_HASH,True)
        committed_ids={e["event"]["event_id"] for e in event_rows}
        forecast_rows,_=read_committed_entities(ancestor/"forecasts.jsonl",committed_ids)
        for stored in forecast_rows:
            if stored["event_id"] not in committed_ids:continue
            f=decode_record(MarketForecast,stored["record"])
            monitor.register_request(f.request_id,f.window_start_nano,f.window_end_nano)
            monitor.publish(f);monitor.published_forecasts[f.request_id]=f
        for event in event_rows:
            source_event=event["event"]
            if source_event["kind"]!="MODEL_ACTIVATION":
                monitor.seen[source_event["event_id"]]=digest(source_event)
                monitor.sequences[source_event["sequence"]]=source_event["event_id"]
                monitor.watermark=max(monitor.watermark,source_event["sequence"])
                if source_event["kind"]=="DISCONNECT":monitor.connected=False
                elif source_event["kind"]=="RECONNECT":monitor.connected=True
    monitor.pending_remaining_lots=dict(checkpoint.pending_remaining_lots)
    monitor.last_account_nano=checkpoint.account.as_of_nano
    cache=MacroCache(bundle.macro_items)
    received_bars={b.bar_id:b for b in bundle.bars if b.available_at_nano is not None and b.available_at_nano<=checkpoint.account.as_of_nano and b.end_nano<=checkpoint.account.as_of_nano}
    states={(s.contract_id,s.strategy_id):s for s in checkpoint.states}
    pending={i.intent_id:i for i in checkpoint.pending_intents}
    processed=set(checkpoint.processed_event_ids)
    records_all=defaultdict(list)
    cohort_start=checkpoint.account.as_of_nano
    async for incoming_event in feed:
        incoming_row=monitor.receive(incoming_event,receiver_clock)
        if incoming_row.runner_received_at_nano>until_nano:break
        ready=monitor.buffered(incoming_row)
        for h,evidence in monitor.clock_evidence.items():
            p=output/"clock_evidence"/(h+".json")
            if not p.exists():atomic_write(p,canonical_json_bytes(evidence))
        for row in ready:
            event=row.event
            if event.event_id in processed or "DUPLICATE" in row.reasons:
                journal.commit(row,checkpoint,{})
                continue
            process_now=receiver_clock.now_nano()
            # Late data is only visible from receiver validation onward. Buffering
            # never permits a decision at an earlier source timestamp.
            if row.visible_bar is not None:
                received_bars[row.visible_bar.bar_id]=row.visible_bar
                monitor.last_bar_end[row.visible_bar.contract_id]=max(monitor.last_bar_end.get(row.visible_bar.contract_id,0),row.visible_bar.end_nano)
            if event.kind=="MACRO_UPDATE":
                items=tuple(replace(i,retrieved_at_nano=max(i.retrieved_at_nano,row.runner_received_at_nano),
                                    available_at_nano=max(i.available_at_nano,process_now+1)) for i in event.macro_items)
                cache.append(items)
            visible_bundle=replace(bundle,bars=tuple(sorted(received_bars.values(),key=lambda b:(b.end_nano,b.bar_id))),
                                   macro_items=tuple(i for i in cache.items if i.available_at_nano<=process_now))
            candidate_ledger=ContinuousLedger.restore(checkpoint,bundle.instruments,bundle.rules,run_policy,lock,registry,models,qhashes)
            candidate_states=dict(states);candidate_pending=dict(pending)
            candidate_checkpoint,records=_process_event(row,visible_bundle,candidate_ledger,candidate_states,candidate_pending,forecast_models,payoff_model,
                                               seasonality_model,regime_reference,specs,run_policy,policy,qualifications,
                                               receiver_clock,monitor,cache,models,qhashes,received_bars)
            ledger=candidate_ledger;states=candidate_states;pending=candidate_pending;checkpoint=candidate_checkpoint
            processed.add(event.event_id)
            for name,items in records.items():records_all[name].extend(items)
            for h,evidence in monitor.clock_evidence.items():
                p=output/"clock_evidence"/(h+".json")
                if not p.exists():atomic_write(p,canonical_json_bytes(evidence))
            journal.commit(row,checkpoint,records)
        await asyncio.sleep(0)
    acceptance=_acceptance(bundle,monitor,records_all,cohort_start,until_nano,receiver_clock.now_nano())
    atomic_write(output/"shadow_acceptance.json",canonical_json_bytes(acceptance))
    files={p.relative_to(output).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.rglob("*")) if p.is_file()}
    manifest={"schema_version":"ak_probability_validation_run_v2.1.0","contract_version":CONTRACT_VERSION,
              "created_at_nano":until_nano,"files":files,"provenance":{
                  "node":"M5","dataset_kind":"SYNTHETIC" if any(e["source_kind"]=="SYNTHETIC" for e in monitor.clock_evidence.values()) else "FORWARD_SHADOW",
                  "input_hashes":[bundle.manifest_sha256,lock],"code_hashes":{},"parameter_hashes":{"run_policy":digest(run_policy)},
                  "model_hashes":dict(models),"policy_versions":{"research":run_policy.research_policy_version,"macro":run_policy.macro_policy_version},
                  "seed":run_policy.seed,"limitations":acceptance["reasons"],"previous_segment_manifest_sha256":previous_manifest_hash,
                  "recovery_source_sha256":recovery_hash,"ignored_tail_sha256":ignored}}
    atomic_write(output/"manifest.json",canonical_json_bytes(manifest))
    return acceptance
