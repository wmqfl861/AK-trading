"""One continuous Decimal account with source-bound valuation and Fill recovery."""
from __future__ import annotations

from copy import copy
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Mapping
import os

from ak_trading.v2.artifacts import canonical_json_bytes, sha256_hex, stable_id
from ak_trading.v2.contracts import (
    AccountSnapshot, DataQuality, Fill, Instrument, LedgerCheckpoint, MarketBar, OrderIntent,
    PositionLot, RunPolicy, StrategyState, TradingRule, ValuationMark,
)
from ak_trading.v2.schemas import encode_record, decode_record, parse_json_bytes
from ak_trading.v2.decision.execution import fee_amount, rule_at

ZERO = Decimal("0")
ZERO_HASH = "0" * 64


def record_hash(record: object) -> str:
    return sha256_hex(canonical_json_bytes(encode_record(record)))


def _checkpoint_hash(checkpoint: LedgerCheckpoint) -> str:
    payload = encode_record(checkpoint)
    del payload["checkpoint_hash"]
    return sha256_hex(canonical_json_bytes(payload))


def _validate_hash(value: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("invalid_dependency_hash")


def _unvalued(contract: str, reasons=("PRICE_MISSING",)) -> ValuationMark:
    return ValuationMark(contract, None, None, None, None, None, None, "UNVALUED", reasons)


class ContinuousLedger:
    def __init__(self, account: AccountSnapshot, instruments: tuple[Instrument, ...],
                 run_policy: RunPolicy, contract_lock_sha256: str,
                 registry_sha256: str, *, exit_registry_sha256: str) -> None:
        if account.lots or any((account.realized_gross_cny, account.fees_cny, account.slippage_cny,
                                account.unrealized_cny, account.margin_cny, account.reserved_margin_cny)):
            raise ValueError("existing_account_requires_checkpoint")
        self._initialize(account, instruments, run_policy, contract_lock_sha256, registry_sha256,
                         exit_registry_sha256)

    def _initialize(self, account, instruments, run_policy, lock, registry,
                    exit_registry=None) -> None:
        _validate_hash(lock)
        _validate_hash(registry)
        if exit_registry is not None:
            _validate_hash(exit_registry)
        if len({i.contract_id for i in instruments}) != len(instruments):
            raise ValueError("duplicate_instruments")
        self._instruments = {i.contract_id: i for i in instruments}
        self._account = account
        self._run_policy = run_policy
        self._lock, self._registry = lock, registry
        self._exit_registry = exit_registry
        self._rules: dict[str, TradingRule] = {}
        self._rule_hashes: dict[str, str] = {}
        self._marks: dict[str, ValuationMark] = {}
        self._fill_hashes: dict[str, str] = {}
        self._events: list[str] = []
        self._event_set: set[str] = set()
        self._capacity: dict[str, int] = {}
        self._model_hashes: dict[str, str] | None = None
        self._qualification_hashes: dict[str, str] = {}
        self._sequence = 0
        self._previous_hash = ZERO_HASH
        self._last_fill_nano = account.as_of_nano
        self._cash_anchor = account.initial_cash_cny
        self._validate_positions(account)

    def _fork(self) -> ContinuousLedger:
        candidate = copy(self)
        for name in ("_rules", "_rule_hashes", "_marks", "_fill_hashes", "_capacity"):
            setattr(candidate, name, dict(getattr(self, name)))
        candidate._events = list(self._events)
        candidate._event_set = set(self._event_set)
        return candidate

    def _validate_positions(self, account: AccountSnapshot) -> None:
        seen = set()
        owners = {}
        for lot in account.lots:
            if lot.contract_id not in self._instruments or lot.lot_id in seen:
                raise ValueError("unknown_contract_or_duplicate_lot")
            seen.add(lot.lot_id)
            owner = (lot.strategy_id, lot.episode_id, lot.side)
            if lot.contract_id in owners and owners[lot.contract_id] != owner:
                raise ValueError("multiple_episode_owners")
            owners[lot.contract_id] = owner
        if min(account.fees_cny, account.slippage_cny, account.margin_cny, account.reserved_margin_cny) < 0:
            raise ValueError("negative_account_cost_or_commitment")

    def snapshot(self, as_of_nano: int) -> AccountSnapshot:
        if as_of_nano < self._account.as_of_nano:
            raise ValueError("snapshot_time_reversal")
        return replace(self._account, as_of_nano=as_of_nano)

    def _consume_rule(self, rule: TradingRule) -> None:
        digest = record_hash(rule)
        if rule.rule_id in self._rule_hashes and self._rule_hashes[rule.rule_id] != digest:
            raise ValueError("consumed_rule_changed")
        self._rules[rule.rule_id] = rule
        self._rule_hashes[rule.rule_id] = digest

    def _contributions(self, lots: tuple[PositionLot, ...]) -> tuple[Decimal, Decimal]:
        unrealized = margin = ZERO
        for lot in lots:
            mark = self._marks[lot.contract_id]
            if mark.status == "UNVALUED":
                continue
            rule = self._rules[mark.rule_id]
            rate = rule.long_margin_rate if lot.side == 1 else rule.short_margin_rate
            if rate is None:
                raise ValueError("valuation_rule_direction_unknown")
            unrealized += lot.side * lot.lots * (mark.price - lot.entry_price) * rule.multiplier
            margin += lot.lots * mark.price * rule.multiplier * rate
        return unrealized, margin

    def _revalue(self, account: AccountSnapshot, as_of_nano: int,
                 trading_day: date, previous: AccountSnapshot) -> AccountSnapshot:
        unrealized, margin = self._contributions(account.lots)
        unvalued = any(m.status == "UNVALUED" for m in self._marks.values())
        stale = any(m.status == "STALE" for m in self._marks.values())
        complete = not (unvalued or stale)
        equity = account.cash_cny + unrealized
        day_start = previous.day_start_equity_cny
        if trading_day != previous.trading_day and previous.quality.status == "READY":
            day_start = previous.equity_cny
        peak = max(previous.peak_equity_cny, equity) if complete else previous.peak_equity_cny
        day_net = equity - day_start
        killed = previous.killed or (complete and (day_net <= -Decimal(".02") * day_start
                                                     or equity <= Decimal(".90") * peak))
        reasons = tuple(sorted({r for m in self._marks.values() for r in m.reasons}))
        status = "MISSING" if unvalued else "STALE" if stale else "READY"
        if previous.quality.status == "INVALID":
            status = "INVALID"
            reasons = tuple(sorted(set(reasons) | set(previous.quality.reasons)))
        payload = [as_of_nano, str(equity), str(margin), [encode_record(l) for l in account.lots],
                   [encode_record(m) for _, m in sorted(self._marks.items())], self._fill_hashes,
                   self._rule_hashes, self._events]
        return replace(account, as_of_nano=as_of_nano, trading_day=trading_day,
            unrealized_cny=unrealized, equity_cny=equity, margin_cny=margin,
            day_start_equity_cny=day_start, day_net_cny=day_net, peak_equity_cny=peak, killed=killed,
            quality=DataQuality(status, reasons, previous.quality.temporal_basis,
                                sha256_hex(canonical_json_bytes(payload))))

    def apply_fill(self, fill: Fill, rule: TradingRule) -> bool:
        digest = record_hash(fill)
        if fill.fill_id in self._fill_hashes:
            if self._fill_hashes[fill.fill_id] != digest:
                raise ValueError("conflicting_duplicate_fill")
            return False
        candidate = self._fork()
        candidate._apply_new_fill(fill, rule, digest)
        self.__dict__.update(candidate.__dict__)
        return True

    def _apply_new_fill(self, fill: Fill, rule: TradingRule, digest: str) -> None:
        if fill.execution_nano < max(self._last_fill_nano, self._account.as_of_nano):
            raise ValueError("out_of_order_fill")
        if fill.contract_id not in self._instruments or fill.rule_id != rule.rule_id \
                or fill.contract_id != rule.contract_id:
            raise ValueError("unknown_fill_owner_or_rule")
        if not (rule.available_at_nano <= fill.execution_nano
                and rule.effective_from_nano <= fill.execution_nano < rule.effective_until_nano) \
                or rule.evidence == "UNKNOWN":
            raise ValueError("COST_UNKNOWN")
        if fill.lots % rule.lot_step or fill.execution_price % rule.price_tick:
            raise ValueError("fill_tick_or_lot_step_invalid")
        leg = {"OPEN": rule.open_fee, "CLOSE": rule.close_fee,
               "CLOSE_TODAY": rule.close_today_fee}[fill.offset]
        if fill.fee_cny != fee_amount(leg, fill.execution_price, fill.lots, rule.multiplier):
            raise ValueError("fee_mismatch")
        if fill.slippage_cny != abs(fill.execution_price - fill.reference_price) * fill.lots * rule.multiplier:
            raise ValueError("slippage_mismatch")
        account = self._account
        if fill.trading_day < account.trading_day:
            raise ValueError("trading_day_reversal")
        lots = list(account.lots)
        realized = account.realized_gross_cny
        existing = [l for l in lots if l.contract_id == fill.contract_id]
        if fill.offset == "OPEN":
            if fill.lots < rule.min_open_lots:
                raise ValueError("minimum_open_lots")
            if any((l.strategy_id, l.episode_id, l.side) != (fill.strategy_id, fill.episode_id, fill.side)
                   for l in existing):
                raise ValueError("position_ownership_mismatch")
            lots.append(PositionLot(stable_id("lot", fill.fill_id), fill.contract_id,
                fill.strategy_id, fill.episode_id, fill.side, fill.lots, fill.execution_nano,
                fill.trading_day, fill.execution_price, fill.fee_cny))
        else:
            owned = sorted((l for l in existing if l.strategy_id == fill.strategy_id
                            and l.episode_id == fill.episode_id and l.side == -fill.side
                            and ((l.opened_trading_day == fill.trading_day) if fill.offset == "CLOSE_TODAY"
                                 else (l.opened_trading_day < fill.trading_day))),
                           key=lambda l: (l.opened_at_nano, l.lot_id))
            if sum(l.lots for l in owned) < fill.lots:
                raise ValueError("unknown_owner_offset_or_excess_close")
            remaining = fill.lots
            for lot in owned:
                closed = min(remaining, lot.lots)
                realized += lot.side * closed * (fill.execution_price - lot.entry_price) * rule.multiplier
                lots.remove(lot)
                if closed < lot.lots:
                    lots.append(replace(lot, lots=lot.lots - closed,
                                        opening_fee_cny=lot.opening_fee_cny * (lot.lots - closed) / lot.lots))
                remaining -= closed
                if not remaining:
                    break
        fees = account.fees_cny + fill.fee_cny
        cash = self._cash_anchor + realized - fees
        self._consume_rule(rule)
        held = {l.contract_id for l in lots}
        self._marks = {c: self._marks.get(c, _unvalued(c)) for c in held}
        self._fill_hashes[fill.fill_id] = digest
        updated = replace(account, lots=tuple(sorted(lots, key=lambda l: (l.opened_at_nano, l.lot_id))),
            realized_gross_cny=realized, fees_cny=fees,
            slippage_cny=account.slippage_cny + fill.slippage_cny, cash_cny=cash,
            equity_cny=cash + account.unrealized_cny,
            day_net_cny=cash + account.unrealized_cny - account.day_start_equity_cny)
        self._account = self._revalue(updated, fill.execution_nano, fill.trading_day, account)
        self._last_fill_nano = fill.execution_nano

    def record_event(self, event_id: str, capacity_used: Mapping[str, int]) -> None:
        if not event_id:
            raise ValueError("empty_event_id")
        if event_id in self._event_set:
            return
        if any(type(v) is not int or v < 0 for v in capacity_used.values()):
            raise ValueError("invalid_capacity_usage")
        for key, amount in capacity_used.items():
            self._capacity[key] = self._capacity.get(key, 0) + amount
        self._events.append(event_id)
        self._event_set.add(event_id)

    def sync_reservations(self, states: tuple[StrategyState, ...],
                          rules: tuple[TradingRule, ...], as_of_nano: int) -> None:
        if as_of_nano < self._account.as_of_nano:
            raise ValueError("reservation_time_reversal")
        reserved = ZERO
        episodes = set()
        candidate = self._fork()
        for state in states:
            budget = state.budget
            if budget is None:
                continue
            if budget.episode_id in episodes:
                raise ValueError("duplicate_episode_budget")
            episodes.add(budget.episode_id)
            if state.phase in ("EXITING", "PAUSED"):
                continue
            if sum(state.layer_filled_lots) >= budget.maximum_lots and not state.pending_intent_ids:
                continue
            rule = rule_at(rules, state.contract_id, as_of_nano)
            if rule is not None:
                candidate._consume_rule(rule)
            actual_lots = tuple(l for l in self._account.lots if l.episode_id == budget.episode_id)
            _, actual = candidate._contributions(actual_lots)
            reserved += max(ZERO, budget.reserved_margin_cny - actual)
        candidate._account = replace(self._account, as_of_nano=as_of_nano, reserved_margin_cny=reserved)
        self.__dict__.update(candidate.__dict__)

    def mark(self, bars: Mapping[str, MarketBar | None], as_of_nano: int,
             trading_day: date, rules: tuple[TradingRule, ...]) -> AccountSnapshot:
        account = self._account
        if as_of_nano < account.as_of_nano or trading_day < account.trading_day:
            raise ValueError("mark_time_reversal")
        for contract, bar in bars.items():
            if contract not in self._instruments:
                raise ValueError("unknown_mark_contract")
            if bar is not None and (not isinstance(bar, MarketBar) or bar.contract_id != contract):
                raise ValueError("invalid_mark_bar_identity")
        candidate = self._fork()
        for contract in {l.contract_id for l in account.lots}:
            old = self._marks.get(contract, _unvalued(contract))
            bar = bars.get(contract)
            rule = rule_at(rules, contract, as_of_nano)
            reasons = []
            if rule is not None:
                candidate._consume_rule(rule)
            sides = {l.side for l in account.lots if l.contract_id == contract}
            valid_rule = rule is not None and rule.evidence != "UNKNOWN" and all(
                (rule.long_margin_rate if side == 1 else rule.short_margin_rate) is not None for side in sides)
            if not valid_rule:
                reasons.append("RULE_MISSING")
            if bar is None:
                reasons.append("PRICE_MISSING")
            elif (not bar.complete or bar.close is None or bar.close <= 0
                  or bar.quality.status != "READY" or bar.available_at_nano is None
                  or not bar.end_nano <= bar.available_at_nano <= as_of_nano):
                reasons.append("PRICE_MISSING")
            elif bar.duration_seconds != 86400 and as_of_nano - bar.end_nano > 120_000_000_000:
                reasons.append("PRICE_STALE")
            elif old.as_of_nano is not None and bar.end_nano < old.as_of_nano:
                reasons.append("PRICE_STALE")
            elif old.source_bar_id == bar.bar_id and old.source_bar_sha256 != record_hash(bar):
                raise ValueError("conflicting_valuation_bar")
            # Replaying exactly the old bar does not cure a missing expected price.
            if bar is not None and old.status == "STALE" and old.source_bar_id == bar.bar_id:
                reasons.extend(old.reasons)
            if reasons:
                candidate._marks[contract] = (replace(old, status="STALE", reasons=tuple(sorted(set(reasons))))
                    if old.price is not None else _unvalued(contract, tuple(sorted(set(reasons)))))
            else:
                candidate._marks[contract] = ValuationMark(contract, bar.close, bar.end_nano, as_of_nano,
                    bar.bar_id, record_hash(bar), rule.rule_id, "CURRENT", ())
        candidate._account = candidate._revalue(account, as_of_nano, trading_day, account)
        self.__dict__.update(candidate.__dict__)
        return self._account

    def checkpoint(self, states: tuple[StrategyState, ...], pending_intents: tuple[OrderIntent, ...],
                   model_hashes: Mapping[str, str], qualification_hashes: Mapping[str, str], *,
                   pending_remaining_lots: Mapping[str, int],
                   exit_registry_sha256: str,
                   exit_bindings: tuple = (),
                   exit_states: tuple = (),
                   exit_execution_links: tuple = ()) -> LedgerCheckpoint:
        self._check_states(states, pending_intents, pending_remaining_lots)
        if self._model_hashes is not None and dict(model_hashes)!=self._model_hashes:
            activation=bool(self._events and self._events[-1].startswith("MODEL_ACTIVATION:"))
            if not activation or self._account.lots or pending_intents or self._account.reserved_margin_cny \
                    or any(s.episode_id is not None or s.budget is not None for s in states):
                raise ValueError("model_change_requires_flat_activation_event")
        if any(k not in qualification_hashes or qualification_hashes[k]!=v for k,v in self._qualification_hashes.items()):
            raise ValueError("consumed_qualification_changed")
        _validate_hash(exit_registry_sha256)
        exit_states=tuple(exit_states)
        if not exit_states:
            # P0 consumer adaptation: with no M3C state machine wired yet, held
            # scopes still require a factual initial ExitState (007 §5.2).
            # Built strictly from ledger facts; M3C later drives real states.
            from ak_trading.v2.contracts import ExitState
            bindings_by_strategy={b.strategy_id:b for b in exit_bindings}
            scopes={}
            for lot in self._account.lots:
                key=(self._account.account_id,lot.contract_id,lot.strategy_id,lot.episode_id)
                scopes.setdefault(key,{"first":lot.opened_at_nano,"fees":ZERO,"side":lot.side})
                entry=scopes[key]
                entry["first"]=min(entry["first"],lot.opened_at_nano)
                entry["fees"]+=lot.opening_fee_cny
            built=[]
            for (account_id,contract_id,strategy_id,episode_id),facts in scopes.items():
                binding=bindings_by_strategy.get(strategy_id)
                if binding is None:
                    raise ValueError("held_scope_without_exit_binding")
                prefix=sha256_hex(canonical_json_bytes(
                    [account_id,contract_id,strategy_id,episode_id,str(facts["first"]),
                     binding.binding_sha256,"initial"]))
                built.append(ExitState(account_id=account_id,contract_id=contract_id,
                    strategy_id=strategy_id,episode_id=episode_id,first_fill_nano=facts["first"],
                    exit_binding_sha256=binding.binding_sha256,realized_gross_cny=ZERO,
                    fees_paid_cny=facts["fees"],price_peak=None,price_peak_source_nano=None,
                    price_peak_source_hash=None,net_peak_r=None,net_peak_source_nano=None,
                    net_peak_source_hash=None,armed=False,protection_stop_price=None,
                    protection_line_version=None,protection_effective_from_nano=None,
                    confirmations=(),no_more_add=False,qbase=None,closed_lots=0,
                    ladder_progress=(),phase="MANAGING",last_committed_event_id=None,
                    state_prefix_hash=prefix))
            exit_states=tuple(sorted(built,key=lambda s:(s.account_id,s.contract_id,
                                                         s.strategy_id,s.episode_id)))
        checkpoint = LedgerCheckpoint(sequence=self._sequence + 1, contract_lock_sha256=self._lock,
            registry_sha256=self._registry, exit_registry_sha256=exit_registry_sha256,
            run_policy=self._run_policy, rule_hashes=self._rule_hashes,
            model_hashes=model_hashes, qualification_hashes=qualification_hashes, account=self._account,
            valuation_marks=tuple(m for _, m in sorted(self._marks.items())),
            states=tuple(sorted(states, key=lambda s: (s.contract_id, s.strategy_id))),
            exit_bindings=tuple(sorted(exit_bindings, key=lambda b: b.binding_sha256)),
            exit_states=tuple(sorted(exit_states, key=lambda s: (s.account_id, s.contract_id,
                                                                s.strategy_id, s.episode_id))),
            exit_execution_links=tuple(sorted(exit_execution_links, key=lambda l: l.link_id)),
            pending_intents=tuple(sorted(pending_intents, key=lambda i: i.intent_id)),
            pending_remaining_lots=pending_remaining_lots, processed_event_ids=tuple(self._events),
            processed_fill_hashes=self._fill_hashes, capacity_used=self._capacity,
            previous_hash=self._previous_hash, checkpoint_hash=ZERO_HASH)
        checkpoint = replace(checkpoint, checkpoint_hash=_checkpoint_hash(checkpoint))
        self._sequence, self._previous_hash = checkpoint.sequence, checkpoint.checkpoint_hash
        self._model_hashes = dict(model_hashes)
        self._qualification_hashes = dict(qualification_hashes)
        return checkpoint

    def _check_states(self, states, pending, remaining) -> None:
        from ak_trading.v2.strategies.state import validate_restored_state
        keys = [(s.contract_id, s.strategy_id) for s in states]
        if len(keys) != len(set(keys)) or len({i.intent_id for i in pending}) != len(pending):
            raise ValueError("duplicate_state_or_intent")
        for state in states:
            validate_restored_state(state, self._account)
        for lot in self._account.lots:
            if not any(s.contract_id == lot.contract_id and s.strategy_id == lot.strategy_id
                       and s.episode_id == lot.episode_id for s in states):
                raise ValueError("position_without_state")
        stated = [i for s in states for i in s.pending_intent_ids]
        if sorted(stated) != sorted(i.intent_id for i in pending) or set(remaining) != set(stated):
            raise ValueError("pending_intent_state_mismatch")
        for intent in pending:
            amount = remaining[intent.intent_id]
            if type(amount) is not int or amount * intent.delta_lots <= 0 or abs(amount) > abs(intent.delta_lots):
                raise ValueError("pending_remaining_invalid")
            if not any(s.contract_id == intent.contract_id and s.strategy_id == intent.strategy_id
                       and s.episode_id == intent.episode_id for s in states):
                raise ValueError("pending_intent_owner_mismatch")

    @classmethod
    def restore(cls, checkpoint: LedgerCheckpoint, instruments: tuple[Instrument, ...],
                rules: tuple[TradingRule, ...], run_policy: RunPolicy,
                contract_lock_sha256: str, registry_sha256: str,
                model_hashes: Mapping[str, str], qualification_hashes: Mapping[str, str], *,
                exit_registry_sha256: str) -> ContinuousLedger:
        if checkpoint.checkpoint_hash != _checkpoint_hash(checkpoint):
            raise ValueError("checkpoint_hash_mismatch")
        if (checkpoint.contract_lock_sha256 != contract_lock_sha256
                or checkpoint.registry_sha256 != registry_sha256 or checkpoint.run_policy != run_policy
                or checkpoint.exit_registry_sha256 != exit_registry_sha256
                or dict(checkpoint.model_hashes) != dict(model_hashes)
                or dict(checkpoint.qualification_hashes) != dict(qualification_hashes)):
            raise ValueError("checkpoint_dependency_mismatch")
        if len(checkpoint.processed_event_ids) != len(set(checkpoint.processed_event_ids)):
            raise ValueError("duplicate_processed_events")
        by_id = {}
        for rule in rules:
            if rule.rule_id in by_id and by_id[rule.rule_id] != rule:
                raise ValueError("conflicting_rule_identity")
            by_id[rule.rule_id] = rule
        for key, digest in checkpoint.rule_hashes.items():
            if key not in by_id or record_hash(by_id[key]) != digest:
                raise ValueError("consumed_rule_hash_mismatch")
        ledger = cls.__new__(cls)
        ledger._initialize(checkpoint.account, instruments, run_policy, contract_lock_sha256,
                           registry_sha256, exit_registry_sha256)
        ledger._check_states(checkpoint.states, checkpoint.pending_intents, checkpoint.pending_remaining_lots)
        ledger._events = list(checkpoint.processed_event_ids)
        ledger._event_set = set(ledger._events)
        ledger._capacity = dict(checkpoint.capacity_used)
        ledger._rule_hashes = dict(checkpoint.rule_hashes)
        ledger._rules = {k: by_id[k] for k in checkpoint.rule_hashes}
        ledger._marks = {m.contract_id: m for m in checkpoint.valuation_marks}
        ledger._fill_hashes = dict(checkpoint.processed_fill_hashes)
        ledger._sequence, ledger._previous_hash = checkpoint.sequence, checkpoint.checkpoint_hash
        ledger._model_hashes = dict(model_hashes)
        ledger._qualification_hashes = dict(qualification_hashes)
        if set(ledger._marks) != {l.contract_id for l in checkpoint.account.lots}:
            raise ValueError("checkpoint_marks_position_mismatch")
        for mark in ledger._marks.values():
            if mark.status != "UNVALUED":
                rule = ledger._rules.get(mark.rule_id)
                if rule is None or not (rule.available_at_nano <= mark.valued_at_nano
                        and rule.effective_from_nano <= mark.valued_at_nano < rule.effective_until_nano):
                    raise ValueError("valuation_rule_reference_invalid")
        unrealized, margin = ledger._contributions(checkpoint.account.lots)
        if (unrealized, margin) != (checkpoint.account.unrealized_cny, checkpoint.account.margin_cny):
            raise ValueError("checkpoint_valuation_reconciliation_failed")
        return ledger

    def reconcile(self) -> Mapping[str, Decimal | bool | str]:
        account = self._account
        cash_diff = account.cash_cny - (self._cash_anchor + account.realized_gross_cny - account.fees_cny)
        unrealized, margin = self._contributions(account.lots)
        equity_diff = account.equity_cny - account.cash_cny - unrealized
        position_match = margin == account.margin_cny and set(self._marks) == {l.contract_id for l in account.lots}
        status = "FAIL" if cash_diff or equity_diff or not position_match else (
            "INCOMPLETE" if account.lots or account.quality.status != "READY" else "PASS")
        return {"cash_difference_cny": cash_diff, "equity_difference_cny": equity_diff,
                "position_match": position_match, "fees_match": cash_diff == ZERO, "status": status}


def publish_checkpoint(path: Path, checkpoint: LedgerCheckpoint) -> None:
    if path.exists() or path.with_suffix(path.suffix + ".tmp").exists():
        raise FileExistsError(path)
    if checkpoint.checkpoint_hash != _checkpoint_hash(checkpoint):
        raise ValueError("checkpoint_hash_mismatch")
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("xb") as handle:
        handle.write(canonical_json_bytes(encode_record(checkpoint)))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def read_checkpoint(path: Path) -> LedgerCheckpoint:
    checkpoint = decode_record(LedgerCheckpoint, parse_json_bytes(path.read_bytes()))
    if checkpoint.checkpoint_hash != _checkpoint_hash(checkpoint):
        raise ValueError("checkpoint_hash_mismatch")
    return checkpoint
