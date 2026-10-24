"""NE1-C1 native contract tests: v2/transport schemas, capability origins, permits.

All identities are SYNTHETIC; no native import, network, .env or file IO beyond
the frozen synthetic fixture shipped with this test.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import json
from pathlib import Path

import pytest

from ak_trading.infiny_execution import contracts as v1
from ak_trading.infiny_execution import native_contracts as nc
from ak_trading.infiny_execution import native_evidence as ne


NOW = datetime(2026, 9, 14, 1, 30, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures" / "infiny_native_execution" / "synthetic_cases.json"


def binding(**changes):
    values = dict(account_key="SIM_ACCOUNT_A", strategy_key="SIM_STRATEGY_S",
                  strategy_id=23, instance_id="SIM_INSTANCE_I", session_epoch="SIM_EPOCH_E",
                  trading_day=NOW.date(), exchange="SHFE",
                  instrument_id="SIM_RB2610", hedge_flag="1")
    return v1.Binding(**(values | changes))


def leg(**changes):
    values = dict(leg_id="SIM_LEG_1", index=0, phase=v1.Phase.OPEN, depends_on=(),
                  direction=v1.Direction.BUY, offset=v1.Offset.OPEN, volume=1,
                  price=Decimal("3500"), position_bucket=None, cash_reserve=Decimal("1000"))
    return v1.Leg(**(values | changes))


def header(**changes):
    values = dict(request_id="SIM_REQ_1", instance_id="SIM_INSTANCE_I", session_epoch="SIM_EPOCH_E",
                  generation="SIM_GEN_1", service_epoch=7, sequence=1, sent_at=NOW,
                  params_sha256="a" * 64)
    return values | changes


def confirmation(**changes):
    values = dict(confirmation_id="SIM_CONFIRM_1", binding=binding(), params_sha256="b" * 64,
                  adapter_sha256="c" * 64, confirmed_at=NOW, expires_at=NOW + timedelta(hours=1),
                  explicit_ui_action=True)
    return nc.ParameterConfirmation(**(values | changes))


def permit(**changes):
    values = dict(permit_id="SIM_PERMIT_1", authorization_id="SIM_AUTH_N1", binding=binding(),
                  control_revision=3, baseline_evidence_id="SIM_BASE_1",
                  route_evidence_id="SIM_ROUTE_1", cash_bound_evidence_id="SIM_CASH_1",
                  source_key="SIM_OPERATION_1",
                  actions=(v1.AuthorizationAction.OPEN_LONG,), max_send_attempts=1,
                  max_cancel_attempts=1, max_volume=1,
                  price_bounds=(v1.PriceBound(symbol=v1.Symbol(exchange="SHFE", instrument_id="SIM_RB2610"),
                                              lower=Decimal("3000"), upper=Decimal("4000")),),
                  expires_at=NOW + timedelta(minutes=5), params_sha256="d" * 64,
                  origin=nc.Origin.SYNTHETIC)
    return nc.NativePilotPermit(**(values | changes))


def offer(**changes):
    values = dict(offer_id="SIM_OFFER_1", intent_id="SIM_INTENT_1", leg_id="SIM_LEG_1",
                  plan_id="e" * 64, intent_hash="f" * 64, leg_hash="1" * 64, binding=binding(),
                  snapshot_id="SIM_SNAPSHOT_1", policy_id="SIM_POLICY_1", params_sha256="2" * 64,
                  control_revision=3, expires_at=NOW + timedelta(milliseconds=500))
    return nc.ExecutionOffer(**(values | changes))


def dispatch(kind=nc.CommandKind.SUBMIT, **changes):
    values = dict(command_id="SIM_CMD_1", kind=kind, attempt_id="SIM_ATTEMPT_1", offer_id="SIM_OFFER_1",
                  intent_id="SIM_INTENT_1", leg_id="SIM_LEG_1", plan_id="e" * 64, binding=binding(),
                  leg=leg(), order_type=v1.OrderType.GFD, price_tick=Decimal("1"),
                  contract_limits=nc.ContractLimit(min_volume=1, max_volume=5, volume_step=1),
                  control_revision=3, permit_id="SIM_PERMIT_1", params_sha256="3" * 64,
                  issued_at=NOW, expires_at=NOW + timedelta(milliseconds=500))
    return nc.DispatchCommand(**(values | changes))


def receipt(stage=nc.ReceiptStage.RETURNED, **changes):
    values = dict(receipt_id="SIM_RECEIPT_1", command_id="SIM_CMD_1", attempt_id="SIM_ATTEMPT_1",
                  binding=binding(), generation="SIM_GEN_1", stage=stage, local_sequence=1,
                  recorded_at=NOW, result_kind=nc.ResultKind.LOCAL_ACK, result_value=7,
                  error_code=None, request_sha256="4" * 64, previous_receipt_sha256=None)
    return nc.DispatchReceipt(**(values | changes))


def callback_event(**changes):
    values = dict(event_id="SIM_EVENT_1", kind=v1.EventKind.ORDER,
                  event_stream_id="SIM_STREAM_1", event_sequence=1,
                  captured_at=NOW, callback_generation="SIM_GEN_1",
                  callback_session_epoch="SIM_EPOCH_E", route_evidence_id="SIM_ROUTE_1",
                  raw_fields=(nc.RawCallbackField(key="OrderID", value="7"),
                              nc.RawCallbackField(key="Direction", value="0")))
    return nc.RawCallbackEnvelope(**(values | changes))


def test_schema_namespaces_are_isolated():
    assert nc.TRANSPORT_SCHEMA_VERSION == "infiny_client_execution_transport_v1_1"
    assert v1.V2_SCHEMA_VERSION == "infiny_execution_v2"
    with pytest.raises(v1.ContractError):
        v1.encode(offer())                      # transport DTO cannot ride the v1 envelope
    with pytest.raises(v1.ContractError):
        v1.encode(nc.ControlState(control_revision=0, binding=binding(), mode=v1.ExecutionMode.DISABLED,
                                  policy_id="SIM_POLICY_1", authorization_id=None, native_permit_id=None,
                                  params_sha256="5" * 64, generation="SIM_GEN_1", updated_at=NOW,
                                  disabled_reason=None))
    wire = nc.encode(dispatch())
    with pytest.raises(v1.ContractError):
        v1.decode(wire, nc.DispatchCommand)     # schema_version mismatch
    assert nc.decode(wire, nc.DispatchCommand) == dispatch()


def test_transport_wire_is_strict_and_stable():
    wire = nc.encode(nc.PollRequest(**header()))
    assert nc.decode(wire, nc.PollRequest) == nc.PollRequest(**header())
    assert wire == nc.encode(nc.PollRequest(**header()))
    packet = json.loads(wire)
    assert packet["schema_version"] == nc.TRANSPORT_SCHEMA_VERSION
    assert packet["type"] == "PollRequest"
    assert nc.canonical_hash(dispatch()) == nc.canonical_hash(dispatch())
    cancel_variant = dispatch(kind=nc.CommandKind.CANCEL, order_id=7,
                              route_evidence_id="SIM_ROUTE_1", cancel_id="SIM_CANCEL_1")
    assert nc.canonical_hash(dispatch()) != nc.canonical_hash(cancel_variant)
    for change in (
        lambda p: p["data"].update(service_epoch=True),
        lambda p: p["data"].update(sent_at="2026-09-14T01:30:00"),
        lambda p: p["data"].pop("params_sha256"),
        lambda p: p["data"].update(sequence=-1),
        lambda p: p["data"].update(params_sha256="not-hex"),
        lambda p: p.update(schema_version="infiny_execution_v2"),
    ):
        packet = json.loads(nc.encode(nc.PollRequest(**header())))
        change(packet)
        with pytest.raises(v1.ContractError):
            nc.decode(json.dumps(packet), nc.PollRequest)
    for bad in ['{"x":1,"x":2}', "NaN", "[1]", b"\xff"]:
        with pytest.raises(v1.ContractError):
            nc.decode(bad, nc.PollRequest)


def test_first_handshake_null_service_epoch_is_representable():
    first = nc.PollRequest(**header(service_epoch=None))
    assert first.service_epoch is None
    assert nc.decode(nc.encode(first), nc.PollRequest) == first


def response_fields(**changes):
    values = dict(request_id="SIM_REQ_1", instance_id="SIM_INSTANCE_I", session_epoch="SIM_EPOCH_E",
                  generation="SIM_GEN_1", sequence=1, params_sha256="a" * 64,
                  service_epoch=7, request_service_epoch=None,
                  sent_at=NOW + timedelta(milliseconds=50), received_at=NOW,
                  binding=binding(), ledger_revision=2, control_revision=3)
    return values | changes


def own_readiness(blockers=(), **changes):
    values = dict(connection=v1.ConnectionState.ROUNDTRIP, state=v1.ReadinessState.PLAN_READY,
                  can_plan=True, can_submit=False, can_cancel_owned=False,
                  blockers=tuple(blockers), mode=v1.ExecutionMode.NATIVE,
                  coverage=own_journal_coverage(), evidence_id="SIM_READY_3",
                  execution_scope=nc.ExecutionScope.NONE, native_permit_id=None)
    return nc.V2Readiness(**(values | changes))


def submit_projection():
    return nc.SubmitProjection(code=v1.ResultCode.OK, leg_id="SIM_LEG_1",
                               leg_state=v1.OrderState.SUBMITTED_LOCAL, attempt_no=1,
                               order_id=7, reported_filled=0, trade_filled=0, risk_filled=0,
                               trade_gap=0, cancel_state=v1.CancelState.NONE, revision=2)


def test_response_family_branches_are_exact():
    idle = nc.PollResponse(**response_fields(), kind=nc.ResponseKind.IDLE, offer=None,
                           readiness=own_readiness(), blockers=())
    assert nc.decode(nc.encode(idle), nc.PollResponse) == idle
    offered = nc.PollResponse(**response_fields(), kind=nc.ResponseKind.OFFER, offer=offer(),
                              readiness=own_readiness(), blockers=())
    assert offered.offer is not None and not offered.blockers
    blocked = nc.PollResponse(**response_fields(), kind=nc.ResponseKind.BLOCKED, offer=None,
                              readiness=own_readiness(blockers=("MODE_DISABLED",)),
                              blockers=("MODE_DISABLED",))
    assert blocked.blockers
    for bad in (
        dict(kind=nc.ResponseKind.OFFER, offer=None, readiness=own_readiness(), blockers=()),
        dict(kind=nc.ResponseKind.IDLE, offer=offer(), readiness=own_readiness(), blockers=()),
        dict(kind=nc.ResponseKind.BLOCKED, offer=None, readiness=own_readiness(),
             blockers=("MODE_DISABLED",)),
    ):
        with pytest.raises(v1.ContractError):
            nc.PollResponse(**response_fields(), **bad)
    with pytest.raises(v1.ContractError):  # blockers must equal readiness.blockers
        nc.PollResponse(**response_fields(), kind=nc.ResponseKind.IDLE, offer=None,
                        readiness=own_readiness(blockers=("MODE_DISABLED",)), blockers=())


def test_ready_results_events_control_status_responses():
    ready = nc.ReadyResponse(**response_fields(), kind=nc.ReadyResponseKind.DISPATCH,
                             dispatch=dispatch(), readiness=own_readiness(), blockers=())
    assert nc.decode(nc.encode(ready), nc.ReadyResponse) == ready
    with pytest.raises(v1.ContractError):
        nc.ReadyResponse(**response_fields(), kind=nc.ReadyResponseKind.BLOCKED,
                         dispatch=None, readiness=own_readiness(), blockers=())
    results = nc.ResultsResponse(**response_fields(), receipt_id="SIM_RECEIPT_1",
                                 command_id="SIM_CMD_1", attempt_id="SIM_ATTEMPT_1",
                                 receipt_sha256="4" * 64, persisted=True, duplicate=False,
                                 submit_result=submit_projection())
    assert nc.decode(nc.encode(results), nc.ResultsResponse) == results
    with pytest.raises(v1.ContractError):
        nc.ResultsResponse(**response_fields(), receipt_id="SIM_RECEIPT_1",
                           command_id="SIM_CMD_1", attempt_id="SIM_ATTEMPT_1",
                           receipt_sha256="4" * 64, persisted=False, duplicate=False,
                           submit_result=submit_projection())
    ack = nc.AckWatermark(event_stream_id="SIM_STREAM_1", instance_id="SIM_INSTANCE_I",
                          session_epoch="SIM_EPOCH_E", generation="SIM_GEN_1", sequence=2,
                          event_id="SIM_EVENT_2", event_sha256="5" * 64)
    events = nc.EventsResponse(**response_fields(), event_stream_id="SIM_STREAM_1",
                               accepted_event_ids=("SIM_EVENT_1", "SIM_EVENT_2"),
                               duplicate_event_ids=("SIM_EVENT_2",), persisted=True,
                               ack_watermark=ack)
    assert nc.decode(nc.encode(events), nc.EventsResponse) == events
    with pytest.raises(v1.ContractError):  # duplicate outside accepted set
        nc.EventsResponse(**response_fields(), event_stream_id="SIM_STREAM_1",
                          accepted_event_ids=("SIM_EVENT_1",), duplicate_event_ids=("SIM_EVENT_9",),
                          persisted=True, ack_watermark=ack)
    control = nc.ControlResponse(**response_fields(), kind=nc.ControlResponseKind.RECORDED,
                                 report_kind=nc.ControlKind.GATE_CLOSED, confirmation_id=None,
                                 control_phase=nc.ControlPhase.DISABLED_CONFIRMED,
                                 readiness=own_readiness(blockers=("EVENT_GAP",)),
                                 blockers=("EVENT_GAP",))
    assert nc.decode(nc.encode(control), nc.ControlResponse) == control
    with pytest.raises(v1.ContractError):  # PARAMS_CONFIRMED must echo the confirmation id
        nc.ControlResponse(**response_fields(), kind=nc.ControlResponseKind.RECORDED,
                           report_kind=nc.ControlKind.PARAMS_CONFIRMED, confirmation_id=None,
                           control_phase=nc.ControlPhase.DISABLED,
                           readiness=own_readiness(), blockers=())
    counters = nc.ExecutionCounters(
        committed_dispatch_count=1, consumed_receipt_count=1, returned_receipt_count=1,
        no_call_receipt_count=0,
        native_send_attempts=nc.CallCount(value=1, lower_bound=1, upper_bound=1),
        native_cancel_attempts=nc.CallCount(value=None, lower_bound=0, upper_bound=None),
        local_order_ids=(7,), broker_order_acks=None, unique_trade_count=None,
        fake_send_count=0, fake_cancel_count=0, unresolved_dispatch_count=0,
        count_complete=False)
    status = nc.StatusResponse(**response_fields(), control_phase=nc.ControlPhase.DISABLED,
                               readiness=own_readiness(), blockers=(), counters=counters,
                               pending_reconcile_count=0, ack_watermark=ack)
    assert nc.decode(nc.encode(status), nc.StatusResponse) == status


def test_call_count_semantics_and_count_complete():
    exact = nc.CallCount(value=1, lower_bound=1, upper_bound=1)
    uncertain = nc.CallCount(value=None, lower_bound=0, upper_bound=None)
    assert exact.value == 1 and uncertain.value is None
    with pytest.raises(v1.ContractError):
        nc.CallCount(value=2, lower_bound=1, upper_bound=1)
    with pytest.raises(v1.ContractError):
        nc.CallCount(value=None, lower_bound=3, upper_bound=2)
    base = dict(committed_dispatch_count=0, consumed_receipt_count=0, returned_receipt_count=0,
                no_call_receipt_count=0,
                native_send_attempts=nc.CallCount(value=None, lower_bound=0, upper_bound=None),
                native_cancel_attempts=nc.CallCount(value=None, lower_bound=0, upper_bound=None),
                local_order_ids=(), broker_order_acks=None, unique_trade_count=None,
                fake_send_count=0, fake_cancel_count=0, unresolved_dispatch_count=0)
    with pytest.raises(v1.ContractError):  # count_complete requires exact real counters
        nc.ExecutionCounters(**base, count_complete=True)
    with pytest.raises(v1.ContractError):
        nc.ExecutionCounters(**(base | {"local_order_ids": (3, 1)}), count_complete=False)
    with pytest.raises(v1.ContractError):
        nc.ExecutionCounters(**(base | {"local_order_ids": (3, 3)}), count_complete=False)


def test_transport_error_table_and_error_response_shape():
    assert nc.TRANSPORT_ERROR_HTTP_STATUS[nc.TransportErrorCode.POLL_TOO_FAST] == 429
    assert len(nc.TRANSPORT_ERROR_HTTP_STATUS) == len(nc.TransportErrorCode)
    throttled = nc.TransportErrorResponse(
        request_id="SIM_REQ_1", instance_id="SIM_INSTANCE_I", session_epoch="SIM_EPOCH_E",
        generation="SIM_GEN_1", sequence=9, params_sha256="a" * 64, service_epoch=7,
        request_service_epoch=7, sent_at=NOW + timedelta(milliseconds=50), received_at=NOW,
        binding=binding(), ledger_revision=2, control_revision=3,
        error_code=nc.TransportErrorCode.POLL_TOO_FAST, http_status=429, retry_after_ms=137)
    assert nc.decode(nc.encode(throttled), nc.TransportErrorResponse) == throttled
    anonymous = nc.TransportErrorResponse(
        request_id=None, instance_id=None, session_epoch=None, generation=None,
        sequence=None, params_sha256=None, service_epoch=7, request_service_epoch=None,
        sent_at=NOW + timedelta(milliseconds=50), received_at=NOW, binding=None,
        ledger_revision=None, control_revision=None,
        error_code=nc.TransportErrorCode.AUTHENTICATION_FAILED, http_status=401,
        retry_after_ms=None)
    assert anonymous.binding is None
    for bad in (
        dict(error_code=nc.TransportErrorCode.POLL_TOO_FAST, http_status=400, retry_after_ms=1),
        dict(error_code=nc.TransportErrorCode.POLL_TOO_FAST, http_status=429, retry_after_ms=0),
    ):
        with pytest.raises(v1.ContractError):
            nc.TransportErrorResponse(
                request_id=None, instance_id=None, session_epoch=None, generation=None,
                sequence=None, params_sha256=None, service_epoch=7, request_service_epoch=None,
                sent_at=NOW, received_at=NOW, binding=None, ledger_revision=None,
                control_revision=None, **bad)


def test_request_fingerprint_is_stable_and_route_bound():
    request = nc.PollRequest(**header())
    first = nc.request_fingerprint("GET", "/execution/v1/poll", request)
    assert first == nc.request_fingerprint("GET", "/execution/v1/poll", request)
    assert first != nc.request_fingerprint("POST", "/execution/v1/poll", request)
    assert first != nc.request_fingerprint("GET", "/execution/v1/events", request)
    assert len(first) == 64
    assert nc.HTTP_REQUEST_FINGERPRINT_VERSION == "infiny_http_request_fingerprint_v1"
    with pytest.raises(v1.ContractError):
        nc.request_fingerprint("get", "/execution/v1/poll", request)
    with pytest.raises(v1.ContractError):
        nc.request_fingerprint("GET", "/execution/v1/poll?x=1", request)


def test_status_header_mapping_is_exact_and_token_free():
    assert nc.STATUS_REQUEST_HEADERS == {
        "schema_version": "X-Execution-Schema-Version",
        "type": "X-Execution-Type",
        "request_id": "X-Execution-Request-Id",
        "instance_id": "X-Execution-Instance-Id",
        "session_epoch": "X-Execution-Session-Epoch",
        "generation": "X-Execution-Generation",
        "service_epoch": "X-Execution-Service-Epoch",
        "sequence": "X-Execution-Sequence",
        "sent_at": "X-Execution-Sent-At",
        "params_sha256": "X-Execution-Params-Sha256",
    }
    assert nc.STATUS_MAX_HEADER_BYTES == 8192
    assert nc.EXECUTION_TOKEN_HEADER not in nc.STATUS_REQUEST_HEADERS.values()


def test_event_cursor_rules():
    empty = nc.EventCursor(event_stream_id="SIM_STREAM_1", instance_id="SIM_INSTANCE_I",
                           session_epoch="SIM_EPOCH_E", generation="SIM_GEN_1", sequence=0,
                           event_id=None, event_sha256=None)
    assert empty.sequence == 0
    positive = nc.EventCursor(event_stream_id="SIM_STREAM_1", instance_id="SIM_INSTANCE_I",
                              session_epoch="SIM_EPOCH_E", generation="SIM_GEN_1", sequence=3,
                              event_id="SIM_EVENT_3", event_sha256="6" * 64)
    assert nc.decode(nc.encode(positive), nc.EventCursor) == positive
    watermark = nc.AckWatermark(event_stream_id="SIM_STREAM_1", instance_id="SIM_INSTANCE_I",
                                session_epoch="SIM_EPOCH_E", generation="SIM_GEN_1", sequence=3,
                                event_id="SIM_EVENT_3", event_sha256="6" * 64)
    assert isinstance(watermark, nc.EventCursor)
    with pytest.raises(v1.ContractError):
        nc.EventCursor(event_stream_id="SIM_STREAM_1", instance_id="SIM_INSTANCE_I",
                       session_epoch="SIM_EPOCH_E", generation="SIM_GEN_1", sequence=0,
                       event_id="SIM_EVENT_0", event_sha256=None)
    with pytest.raises(v1.ContractError):
        nc.EventCursor(event_stream_id="SIM_STREAM_1", instance_id="SIM_INSTANCE_I",
                       session_epoch="SIM_EPOCH_E", generation="SIM_GEN_1", sequence=3,
                       event_id=None, event_sha256="6" * 64)


def test_dispatch_command_kind_specific_keys():
    assert nc.DISPATCH_TTL_MS == 1500
    submit = dispatch()
    assert submit.order_id is None and submit.cancel_id is None
    cancel_keys = dict(order_id=7, route_evidence_id="SIM_ROUTE_1", cancel_id="SIM_CANCEL_1")
    cancelled = dispatch(kind=nc.CommandKind.CANCEL, **cancel_keys)
    assert nc.decode(nc.encode(cancelled), nc.DispatchCommand) == cancelled
    with pytest.raises(v1.ContractError):
        dispatch(order_id=7)                     # SUBMIT cannot carry cancel keys
    for missing in ("order_id", "route_evidence_id", "cancel_id"):
        keys = dict(order_id=7, route_evidence_id="SIM_ROUTE_1", cancel_id="SIM_CANCEL_1")
        keys[missing] = None
        with pytest.raises(v1.ContractError):
            dispatch(kind=nc.CommandKind.CANCEL, **keys)
    with pytest.raises(v1.ContractError):
        dispatch(expires_at=NOW)                 # non-positive validity window
    with pytest.raises(v1.ContractError):
        dispatch(price_tick=Decimal("0.3"))      # leg price not on tick grid


def test_dispatch_receipt_stages_carry_exact_result_semantics():
    assert nc.decode(nc.encode(receipt()), nc.DispatchReceipt) == receipt()
    no_call = receipt(stage=nc.ReceiptStage.NO_CALL, result_kind=None, result_value=None)
    assert no_call.result_kind is None
    rejected = receipt(result_kind=nc.ResultKind.LOCAL_REJECTED, result_value=None)
    assert rejected.result_value is None         # never fabricate -1 as a value
    with pytest.raises(v1.ContractError):
        receipt(stage=nc.ReceiptStage.NO_CALL, result_kind=nc.ResultKind.LOCAL_ACK)
    with pytest.raises(v1.ContractError):
        receipt(result_kind=nc.ResultKind.LOCAL_ACK, result_value=None)
    with pytest.raises(v1.ContractError):
        receipt(result_kind=nc.ResultKind.EXCEPTION, error_code=None)
    with pytest.raises(v1.ContractError):
        receipt(local_sequence=True)


def test_event_batch_is_single_stream_with_unique_sequences():
    events = tuple(callback_event(event_id=f"SIM_EVENT_{i}", event_sequence=i + 1)
                   for i in range(nc.MAX_EVENTS_PER_BATCH))
    batch = nc.EventBatch(**header(), events=events)
    assert nc.decode(nc.encode(batch), nc.EventBatch) == batch
    with pytest.raises(v1.ContractError):
        nc.EventBatch(**header(), events=events + (
            callback_event(event_id="SIM_EVENT_X", event_sequence=129),))
    with pytest.raises(v1.ContractError):
        nc.EventBatch(**header(), events=())
    with pytest.raises(v1.ContractError):  # duplicate sequence, unique id
        nc.EventBatch(**header(), events=(
            callback_event(event_id="SIM_EVENT_A", event_sequence=1),
            callback_event(event_id="SIM_EVENT_B", event_sequence=1)))
    with pytest.raises(v1.ContractError):  # second stream inside one batch
        nc.EventBatch(**header(), events=(
            callback_event(event_id="SIM_EVENT_A", event_sequence=1),
            callback_event(event_id="SIM_EVENT_B", event_sequence=2,
                           event_stream_id="SIM_STREAM_2")))
    with pytest.raises(v1.ContractError):
        nc.EventBatch(**header(), events=(callback_event(event_sequence=0),))
    with pytest.raises(v1.ContractError):
        nc.EventBatch(**header(), events=(callback_event(
            event_id="SIM_EVENT_0", event_sequence=1,
            raw_fields=tuple(nc.RawCallbackField(key=f"K{i}", value="v") for i in range(65))),))


def test_parameter_confirmation_requires_explicit_ui_action():
    assert confirmation().explicit_ui_action is True
    with pytest.raises(v1.ContractError):
        confirmation(explicit_ui_action=False)
    with pytest.raises(v1.ContractError):
        confirmation(expires_at=NOW)             # expiry must follow confirmation


def test_permit_attempts_are_positive_and_origin_tracked():
    item = permit()
    assert item.max_send_attempts == 1 and item.origin is nc.Origin.SYNTHETIC
    assert nc.decode(nc.encode(item), nc.NativePilotPermit) == item
    with pytest.raises(v1.ContractError):
        permit(max_send_attempts=0)
    with pytest.raises(v1.ContractError):
        permit(max_cancel_attempts=-1)
    with pytest.raises(v1.ContractError):
        permit(actions=())


def readiness_v2(scope, **changes):
    values = dict(connection=v1.ConnectionState.ROUNDTRIP, state=v1.ReadinessState.EXECUTABLE,
                  can_plan=True, can_submit=False, can_cancel_owned=False, blockers=(),
                  mode=v1.ExecutionMode.NATIVE, coverage=None, evidence_id="SIM_READY_2",
                  execution_scope=scope, native_permit_id=None)
    return nc.V2Readiness(**(values | changes))


def own_journal_coverage():
    return v1.Coverage(level=v1.CoverageLevel.OWN_JOURNAL, account_key="SIM_ACCOUNT_A",
                       strategy_key="SIM_STRATEGY_S", session_epoch="SIM_EPOCH_E",
                       trading_day=NOW.date(), source=v1.Source.PYTHONGO, as_of=NOW,
                       complete_through=None, completion_evidence_id=None,
                       own_active_count=0, account_active_count=None)


def test_v2_readiness_native_scope_requires_registered_permit():
    blocked = readiness_v2(nc.ExecutionScope.NATIVE_PILOT)
    assert blocked.can_submit is False and blocked.native_permit_id is None
    armed = readiness_v2(nc.ExecutionScope.NATIVE_PILOT, can_submit=True,
                         native_permit_id="SIM_PERMIT_1", coverage=own_journal_coverage())
    assert armed.can_submit is True
    assert armed.coverage.level is not v1.CoverageLevel.FULL_ACCOUNT  # pilot never upgrades coverage
    with pytest.raises(v1.ContractError):
        readiness_v2(nc.ExecutionScope.NATIVE_PILOT, can_submit=True, native_permit_id="SIM_PERMIT_1",
                     coverage=own_journal_coverage(), blockers=("BASELINE_GAP",))  # armed needs zero blockers
    with pytest.raises(v1.ContractError):
        readiness_v2(nc.ExecutionScope.NATIVE_PILOT, can_submit=True,
                     coverage=own_journal_coverage())            # permit reference required
    with pytest.raises(v1.ContractError):
        readiness_v2(nc.ExecutionScope.NONE, native_permit_id="SIM_PERMIT_1")
    with pytest.raises(v1.ContractError):
        readiness_v2(nc.ExecutionScope.NATIVE_PILOT, mode=v1.ExecutionMode.SIMULATION)
    with pytest.raises(v1.ContractError):
        readiness_v2(nc.ExecutionScope.SIMULATION_FULL_ACCOUNT, mode=v1.ExecutionMode.NATIVE)


def test_control_state_is_the_cas_record():
    base = dict(binding=binding(), mode=v1.ExecutionMode.NATIVE, policy_id="SIM_POLICY_1",
                authorization_id="SIM_AUTH_N1", native_permit_id="SIM_PERMIT_1",
                params_sha256="6" * 64, generation="SIM_GEN_1", updated_at=NOW, disabled_reason=None)
    current = nc.ControlState(control_revision=5, **base)
    assert nc.canonical_hash(current) == nc.canonical_hash(nc.ControlState(control_revision=5, **base))
    assert nc.canonical_hash(current) != nc.canonical_hash(nc.ControlState(control_revision=6, **base))
    with pytest.raises(v1.ContractError):
        nc.ControlState(control_revision=5, mode=v1.ExecutionMode.NATIVE, native_permit_id=None,
                        binding=binding(), policy_id="SIM_POLICY_1", authorization_id=None,
                        params_sha256="6" * 64, generation="SIM_GEN_1", updated_at=NOW,
                        disabled_reason=None)                      # native control needs the permit
    with pytest.raises(v1.ContractError):
        nc.ControlState(control_revision=5, mode=v1.ExecutionMode.SIMULATION,
                        native_permit_id="SIM_PERMIT_1", binding=binding(), policy_id="SIM_POLICY_1",
                        authorization_id=None, params_sha256="6" * 64, generation="SIM_GEN_1",
                        updated_at=NOW, disabled_reason=None)      # simulation cannot carry native permit


def test_native_authorization_counts_attempts_separately():
    auth = nc.NativeAuthorization(authorization_id="SIM_AUTH_N1", binding=binding(),
                                  symbols=(v1.Symbol(exchange="SHFE", instrument_id="SIM_RB2610"),),
                                  actions=(v1.AuthorizationAction.OPEN_LONG,
                                           v1.AuthorizationAction.CANCEL_OWNED),
                                  max_send_attempts=1, max_cancel_attempts=1, max_volume=1,
                                  price_bounds=(), expires_at=NOW + timedelta(minutes=5),
                                  origin=nc.Origin.SYNTHETIC, params_sha256="7" * 64)
    assert nc.decode(nc.encode(auth), nc.NativeAuthorization) == auth
    with pytest.raises(v1.ContractError):
        nc.NativeAuthorization(authorization_id="SIM_AUTH_N1", binding=binding(), symbols=(),
                               actions=(v1.AuthorizationAction.OPEN_LONG,), max_send_attempts=1,
                               max_cancel_attempts=0, max_volume=1, price_bounds=(),
                               expires_at=NOW + timedelta(minutes=5), origin=nc.Origin.PRODUCTION,
                               params_sha256="7" * 64)             # empty symbols rejected
    with pytest.raises(v1.ContractError):
        nc.NativeAuthorization(authorization_id="SIM_AUTH_N1", binding=binding(),
                               symbols=(v1.Symbol(exchange="SHFE", instrument_id="SIM_RB2610"),),
                               actions=(), max_send_attempts=1, max_cancel_attempts=0, max_volume=1,
                               price_bounds=(), expires_at=NOW + timedelta(minutes=5),
                               origin=nc.Origin.PRODUCTION, params_sha256="7" * 64)


def baseline(**changes):
    values = dict(evidence_id="SIM_BASE_1", source_kind=ne.EvidenceSourceKind.TERMINAL_EXPORT,
                  artifact_sha256="8" * 64, account_key="SIM_ACCOUNT_A", trading_day=NOW.date(),
                  session_epoch="SIM_EPOCH_E", as_of=NOW, order_inventory=(), trade_inventory=(),
                  positions=(), coverage_description="terminal export covering orders and trades",
                  completion_basis="export row counts reconciled with terminal listing",
                  isolation_evidence_id="SIM_ISO_1", approved_scope_id="SIM_SCOPE_1",
                  expires_at=NOW + timedelta(minutes=5), origin=nc.Origin.SYNTHETIC)
    return ne.NativeBaselineEvidence(**(values | changes))


def route_evidence(**changes):
    values = dict(evidence_id="SIM_ROUTE_1", framework_sha256="9" * 64, sdk_sha256="a" * 64,
                  binding=binding(), callback_kinds=(ne.CallbackKind.ON_ORDER, ne.CallbackKind.ON_TRADE),
                  account_scope_basis="verified callback account filter", strategy_scope_basis="strategy_id match",
                  local_alias_scope="SIM_EPOCH_E", front_id=11, session_id=22, memo_limit=32,
                  memo_roundtrip_verified=True, validated_at=NOW,
                  expires_at=NOW + timedelta(hours=1), origin=nc.Origin.SYNTHETIC)
    return ne.NativeRouteEvidence(**(values | changes))


def thread_evidence(**changes):
    values = dict(evidence_id="SIM_THREAD_1", instance_id="SIM_INSTANCE_I", session_epoch="SIM_EPOCH_E",
                  generation="SIM_GEN_1", process_instance_id="SIM_PROCESS_1", callback_thread_id=4242,
                  callback_kind=ne.CallbackKind.ON_TICK, observed_at=NOW, origin=nc.Origin.SYNTHETIC)
    return ne.ClientThreadEvidence(**(values | changes))


def settlement(**changes):
    values = dict(evidence_id="SIM_SETTLE_1", baseline_evidence_id="SIM_BASE_1", binding=binding(),
                  as_of=NOW, artifact_sha256="b" * 64, covered_leg_ids=("SIM_LEG_1",),
                  covered_event_ids=("SIM_EVENT_1",), order_inventory=(), trade_inventory=(),
                  position_snapshot_id="SIM_SNAPSHOT_2",
                  completion_basis="terminal export rows for the scoped leg",
                  isolation_evidence_id="SIM_ISO_1", origin=nc.Origin.SYNTHETIC)
    return ne.NativeSettlementEvidence(**(values | changes))


def test_baseline_source_kind_whitelist_and_expiry():
    item = baseline()
    assert nc.decode(nc.encode(item), ne.NativeBaselineEvidence) == item
    wire = json.loads(nc.encode(item))
    wire["data"]["source_kind"] = "MANUAL_DECLARATION"
    with pytest.raises(v1.ContractError):
        nc.decode(json.dumps(wire), ne.NativeBaselineEvidence)
    expired = baseline(expires_at=NOW + timedelta(milliseconds=1))
    assert "BASELINE_EXPIRED" in ne.validate_baseline(expired, now=NOW + timedelta(milliseconds=2)).blockers
    assert ne.validate_baseline(item, now=NOW).ok


def query_log_order_item(**changes):
    values = dict(account_key="SIM_ACCOUNT_A", trading_day=NOW.date(), exchange="SHFE",
                  instrument_id="SIM_RB2610", hedge_flag="1", direction=v1.Direction.BUY,
                  offset=v1.Offset.OPEN, order_id=1, order_sys_id="SIM_SYS_1",
                  total_volume=1, traded_volume=0, cancel_volume=0, active=True)
    return v1.OrderInventoryItem(**(values | changes))


def query_log_trade_item(**changes):
    values = dict(account_key="SIM_ACCOUNT_A", trading_day=NOW.date(), exchange="SHFE",
                  instrument_id="SIM_RB2610", hedge_flag="1", trade_id="SIM_TRADE_1",
                  order_sys_id="SIM_SYS_1", order_id=1, direction=v1.Direction.BUY,
                  offset=v1.Offset.OPEN, price=Decimal("3500"), volume=1)
    return v1.TradeInventoryItem(**(values | changes))


def query_log_position_bucket(**changes):
    values = dict(binding=binding(), side=v1.Side.LONG,
                  timing=v1.ComponentTiming(sampled_at=NOW, source_at=NOW, received_at=None,
                                            snapshot_id="SIM_SNAPSHOT_1", source=v1.Source.PYTHONGO,
                                            completeness=v1.Completeness.COMPLETE),
                  Position=1, PositionClose=1, FrozenClosing=0, YdPositionClose=0,
                  YdFrozenClosing=0)
    return v1.PositionBucket(**(values | changes))


def query_log_baseline(**changes):
    """SYNTHETIC SIMULATION_TERMINAL_QUERY_LOG sample (BS-1 skeleton, ruling
    NE1-ARB-BASELINE-SOURCE-001 §4 field mapping). Every value is SYNTHETIC;
    production registration still refuses this origin."""
    values = dict(evidence_id="SIM_BASE_QUERY_LOG_SYNTHETIC_1",
                  source_kind=ne.EvidenceSourceKind.SIMULATION_TERMINAL_QUERY_LOG,
                  artifact_sha256="e" * 64, account_key="SIM_ACCOUNT_A", trading_day=NOW.date(),
                  session_epoch="SIM_EPOCH_E", as_of=NOW,
                  order_inventory=(query_log_order_item(),),
                  trade_inventory=(query_log_trade_item(),),
                  positions=(query_log_position_bucket(),),
                  coverage_description=f"CTP_SIMULATION;NON_LIVE;account=SIM_ACCOUNT_A "
                                       f"trading_day={NOW.date().isoformat()} full query group "
                                       f"ACCOUNT/ORDERS/TRADES/POSITIONS",
                  completion_basis="completed query-group receipt sha256=" + "9" * 64,
                  isolation_evidence_id="SIM_ISO_1", approved_scope_id="SIM_SCOPE_1",
                  expires_at=NOW + timedelta(minutes=5), origin=nc.Origin.SYNTHETIC)
    return ne.NativeBaselineEvidence(**(values | changes))


def test_sim_query_log_source_kind_is_accepted_by_the_new_codec():
    assert ne.EvidenceSourceKind.SIMULATION_TERMINAL_QUERY_LOG.value == \
        "SIMULATION_TERMINAL_QUERY_LOG"
    assert {kind.value for kind in ne.EvidenceSourceKind} == {
        "TERMINAL_EXPORT", "BROKER_EXPORT", "SIMULATION_TERMINAL_QUERY_LOG"}
    item = query_log_baseline()
    assert item.source_kind is ne.EvidenceSourceKind.SIMULATION_TERMINAL_QUERY_LOG
    wire = nc.encode(item)
    assert json.loads(wire)["data"]["source_kind"] == "SIMULATION_TERMINAL_QUERY_LOG"
    assert nc.decode(wire, ne.NativeBaselineEvidence) == item
    # legacy values keep their meaning: existing evidence roundtrips unchanged
    legacy = baseline()
    assert json.loads(nc.encode(legacy))["data"]["source_kind"] == "TERMINAL_EXPORT"
    assert nc.decode(nc.encode(legacy), ne.NativeBaselineEvidence) == legacy


def test_legacy_codecs_reject_the_new_source_kind():
    class LegacySourceKind(str, Enum):  # a codec frozen before BS-1 knows two values
        TERMINAL_EXPORT = "TERMINAL_EXPORT"
        BROKER_EXPORT = "BROKER_EXPORT"

    with pytest.raises(ValueError):  # the ruling: old codecs must reject the value
        LegacySourceKind("SIMULATION_TERMINAL_QUERY_LOG")
    packet = json.loads(nc.encode(query_log_baseline()))
    for bad in ("simulation_terminal_query_log",          # case-sensitive wire value
                "SIMULATION_TERMINAL_QUERY_LOGS",         # near miss
                "TERMINAL_QUERY_LOG"):                    # relabeling attempt
        packet["data"]["source_kind"] = bad
        with pytest.raises(v1.ContractError):
            nc.decode(json.dumps(packet), ne.NativeBaselineEvidence)


def test_sim_query_log_skeleton_rejection_matrix():
    validator = ne.validate_simulation_terminal_query_log_baseline
    # missing environment annotation in coverage_description (§4: must carry
    # CTP_SIMULATION;NON_LIVE and bind the baseline's own account)
    for bad_coverage in ("full query group rows only",
                         "NON_LIVE;account=SIM_ACCOUNT_A all groups",
                         "CTP_SIMULATION;account=SIM_ACCOUNT_A all groups",
                         "CTP_SIMULATION;NON_LIVE;account=SIM_OTHER"):
        result = validator(query_log_baseline(coverage_description=bad_coverage), now=NOW)
        assert result.blockers == ("BASELINE_INSUFFICIENT_COVERAGE_ENV",)
        assert not result.ok
    # completion basis without a real sha256 receipt reference
    slogan = validator(query_log_baseline(completion_basis="user confirmed all zero"), now=NOW)
    assert slogan.blockers == ("BASELINE_INSUFFICIENT_COMPLETION_BASIS",)
    # empty inventories are explicit INSUFFICIENT placeholders, never
    # default-allowed zero rows (zero-row proof belongs to BS-2/BS-3)
    for field in ("order_inventory", "trade_inventory", "positions"):
        result = validator(query_log_baseline(**{field: ()}), now=NOW)
        assert result.blockers == ("BASELINE_INSUFFICIENT_INVENTORY",)
    # inventory items outside the proven query domain (account/trading day)
    wrong_account = validator(query_log_baseline(
        order_inventory=(query_log_order_item(account_key="SIM_ACCOUNT_B"),)), now=NOW)
    assert wrong_account.blockers == ("BASELINE_INSUFFICIENT_INVENTORY_SCOPE",)
    foreign_bucket = validator(query_log_baseline(
        positions=(query_log_position_bucket(
            binding=binding(account_key="SIM_ACCOUNT_B", instrument_id="SIM_RB2601")),)), now=NOW)
    assert foreign_bucket.blockers == ("BASELINE_INSUFFICIENT_INVENTORY_SCOPE",)
    # expired approval window
    expired = validator(query_log_baseline(expires_at=NOW + timedelta(milliseconds=1)),
                        now=NOW + timedelta(milliseconds=2))
    assert expired.blockers == ("BASELINE_EXPIRED",)
    # a missing artifact (frozen-original) reference never reaches the
    # validator: the codec rejects the wire outright
    packet = json.loads(nc.encode(query_log_baseline()))
    del packet["data"]["artifact_sha256"]
    with pytest.raises(v1.ContractError):
        nc.decode(json.dumps(packet), ne.NativeBaselineEvidence)
    # every missing condition accumulates: no default pass through the generic gate
    worst = validator(query_log_baseline(
        coverage_description="no annotation", completion_basis="no hash",
        order_inventory=(), trade_inventory=(), positions=(),
        expires_at=NOW + timedelta(milliseconds=1)), now=NOW + timedelta(milliseconds=2))
    assert set(worst.blockers) == {"BASELINE_INSUFFICIENT_COVERAGE_ENV",
                                   "BASELINE_INSUFFICIENT_COMPLETION_BASIS",
                                   "BASELINE_INSUFFICIENT_INVENTORY",
                                   "BASELINE_EXPIRED"}
    assert not ne.validate_baseline(query_log_baseline(coverage_description="no annotation"),
                                    now=NOW).ok  # generic path fails closed too
    # the skeleton is source-kind specific: legacy kinds keep the old meaning
    with pytest.raises(v1.ContractError):
        validator(baseline(), now=NOW)
    assert ne.validate_baseline(baseline(), now=NOW).ok


def test_sim_query_log_positive_synthetic_sample_passes_the_skeleton():
    item = query_log_baseline()
    assert item.origin is nc.Origin.SYNTHETIC  # SYNTHETIC-labeled sample
    assert ne.validate_simulation_terminal_query_log_baseline(item, now=NOW).ok
    assert ne.validate_baseline(item, now=NOW).ok
    assert nc.decode(nc.encode(item), ne.NativeBaselineEvidence) == item
    # a SYNTHETIC sample can never enter the production registry
    with pytest.raises(v1.ContractError):
        ne.ProductionEvidenceRegistry().register_baseline(item)


def test_route_evidence_needs_ownership_combination():
    assert ne.validate_route(route_evidence(), now=NOW).ok
    with pytest.raises(v1.ContractError):
        route_evidence(front_id=None, session_id=None, local_alias_scope="")
    with pytest.raises(v1.ContractError):
        route_evidence(account_scope_basis="")
    with pytest.raises(v1.ContractError):
        route_evidence(callback_kinds=())
    assert ne.validate_route(route_evidence(), now=NOW + timedelta(hours=2)).blockers


def test_thread_evidence_stays_in_its_process_domain():
    item = thread_evidence()
    assert nc.decode(nc.encode(item), ne.ClientThreadEvidence) == item
    with pytest.raises(v1.ContractError):
        thread_evidence(process_instance_id="")
    with pytest.raises(v1.ContractError):
        thread_evidence(callback_thread_id=True)


def test_settlement_evidence_scope_lists_are_unique():
    item = settlement()
    assert nc.decode(nc.encode(item), ne.NativeSettlementEvidence) == item
    with pytest.raises(v1.ContractError):
        settlement(covered_leg_ids=("SIM_LEG_1", "SIM_LEG_1"))
    with pytest.raises(v1.ContractError):
        settlement(covered_event_ids=())
    assert ne.validate_settlement(item, now=NOW).ok


def test_production_registry_refuses_everything_and_synthetic_permits():
    registry = ne.ProductionEvidenceRegistry()
    with pytest.raises(v1.ContractError):
        registry.register_baseline(baseline())
    with pytest.raises(v1.ContractError):
        registry.register_permit(permit())
    assert not registry.verify_permit_active("SIM_PERMIT_1", now=NOW).ok
    factory = ne.TestEvidenceFactory(purpose="SYNTHETIC_TEST_ONLY")
    synthetic_permit = factory.issue_permit(permit_id="SIM_PERMIT_2", authorization_id="SIM_AUTH_N1",
                                            binding=binding(), control_revision=1,
                                            baseline_evidence_id="SIM_BASE_1",
                                            route_evidence_id="SIM_ROUTE_1",
                                            cash_bound_evidence_id="SIM_CASH_1",
                                            source_key="SIM_OPERATION_1",
                                            actions=(v1.AuthorizationAction.OPEN_LONG,),
                                            max_volume=1, expires_at=NOW + timedelta(minutes=5),
                                            params_sha256="c" * 64)
    assert synthetic_permit.origin is nc.Origin.SYNTHETIC
    assert not registry.verify_permit_active(synthetic_permit.permit_id, now=NOW).ok


def test_frozen_size_and_rate_constants():
    assert nc.MAX_REQUEST_BYTES == 256 * 1024
    assert nc.MAX_RESPONSE_BYTES == 64 * 1024
    assert nc.MAX_NESTING == 32
    assert nc.MAX_EVENTS_PER_BATCH == 128
    assert nc.DISPATCH_TTL_MS == 1500
    assert nc.POLL_ARMED_MIN_INTERVAL_MS == 250
    assert nc.POLL_DISABLED_INTERVAL_MS == 5000
    assert nc.INITIAL_SERVICE_EPOCH == 1
    assert nc.EVENT_SEQUENCE_START == 1
    assert nc.MAX_HTTP_WORKERS == 8
    assert nc.MAX_ACCOUNT_ACTORS == 8
    assert nc.MAX_ARMED_NATIVE_ACCOUNTS == 1
    assert nc.EXECUTION_TOKEN_HEADER == "X-Execution-Token"
    assert nc.PAIRING_TOKEN_BYTES == 32
    assert nc.PAIRING_TOKEN_HEX_LENGTH == 64


def poll_snapshot():
    """A by-value snapshot whose client-supplied received_at fields are null."""
    timing = dict(sampled_at=NOW, source_at=NOW, received_at=None,
                  snapshot_id="SIM_SNAPSHOT_1", source=v1.Source.SIMULATION,
                  completeness=v1.Completeness.COMPLETE)
    quote_timing = v1.ComponentTiming(**{**timing, "source_at": NOW})
    base = binding()
    coverage = v1.Coverage(level=v1.CoverageLevel.FULL_ACCOUNT, account_key=base.account_key,
                           strategy_key=None, session_epoch=base.session_epoch,
                           trading_day=base.trading_day, source=v1.Source.SIMULATION,
                           as_of=NOW,
                           complete_through=v1.SourceWatermark(source=v1.Source.SIMULATION,
                                                               stream_id="SIM_STREAM_0",
                                                               value="17", as_of=NOW),
                           completion_evidence_id="SIM_BARRIER_1", own_active_count=0,
                           account_active_count=0)
    return v1.ExecutionSnapshot(
        snapshot_id="SIM_SNAPSHOT_1", binding=base, source=v1.Source.SIMULATION,
        times=v1.SnapshotTimes(sampled_at=NOW, sent_at=None, received_at=None,
                               quote=quote_timing, contract=v1.ComponentTiming(**timing),
                               account=v1.ComponentTiming(**timing),
                               positions=v1.ComponentTiming(**timing)),
        quote=v1.Quote(binding=base, time=NOW, last=Decimal("3500"), bid1=Decimal("3500"),
                       ask1=Decimal("3501"), bid_volume1=10, ask_volume1=10,
                       upper_limit=Decimal("4000"), lower_limit=Decimal("3000"),
                       market_status=v1.MarketStatus.CONTINUOUS),
        contract=v1.Contract(exchange=base.exchange, instrument_id=base.instrument_id,
                             asset_class=v1.AssetClass.FUTURE, expiry=NOW.date() + timedelta(days=30),
                             price_tick=Decimal("1"), multiplier=Decimal("10"),
                             min_limit_order_volume=1, max_limit_order_volume=5),
        account=v1.Account(binding=base, available=Decimal("100000"), currency="CNY"),
        positions=(), coverage=coverage, proof_id=None, ledger_revision=0)


def test_poll_request_transports_snapshot_by_value():
    snap = poll_snapshot()
    request = nc.PollRequest(**header(), snapshot=snap, snapshot_id="SIM_SNAPSHOT_1")
    assert nc.decode(nc.encode(request), nc.PollRequest) == request
    with pytest.raises(v1.ContractError):  # snapshot_id without the value
        nc.PollRequest(**header(), snapshot_id="SIM_SNAPSHOT_1")
    with pytest.raises(v1.ContractError):  # mismatched id
        nc.PollRequest(**header(), snapshot=snap, snapshot_id="SIM_OTHER")
    stamped = replace(snap.times, received_at=NOW)
    with pytest.raises(v1.ContractError):  # client cannot stamp received_at
        nc.PollRequest(**header(), snapshot=replace(snap, times=stamped),
                       snapshot_id="SIM_SNAPSHOT_1")
    first = nc.PollRequest(**header(service_epoch=None))
    assert first.snapshot is None and first.snapshot_id is None and first.service_epoch is None


def test_ready_challenge_carries_event_cursor():
    cursor = nc.EventCursor(event_stream_id="SIM_STREAM_1", instance_id="SIM_INSTANCE_I",
                            session_epoch="SIM_EPOCH_E", generation="SIM_GEN_1", sequence=5,
                            event_id="SIM_EVENT_5", event_sha256="7" * 64)
    challenge = nc.ReadyChallenge(offer_id="SIM_OFFER_1", capture_id="SIM_SNAPSHOT_1",
                                  thread_evidence_id="SIM_THREAD_1",
                                  latest_params_sha256="b" * 64, enable_trading=True,
                                  unconfirmed_event_watermark=cursor, generation="SIM_GEN_1")
    assert nc.decode(nc.encode(challenge), nc.ReadyChallenge) == challenge
    request = nc.ReadyRequest(**header(), offer_id="SIM_OFFER_1", challenge=challenge)
    assert nc.decode(nc.encode(request), nc.ReadyRequest) == request


def manifest(**changes):
    values = dict(registration_id="SIM_REG_1",
                  record_type=ne.OperatorRecordType.NATIVE_PILOT_PERMIT,
                  record_path=r"C:\sim\records\permit.json",
                  record_sha256="c" * 64, contract_lock_sha256="6" * 64,
                  operator_action_id="SIM_ACTION_1",
                  authorization_artifact_path=r"C:\sim\authz.txt",
                  authorization_artifact_sha256="d" * 64,
                  artifacts=(ne.OperatorArtifact(path=r"C:\sim\records\permit.json",
                                                 sha256="c" * 64),))
    return ne.OperatorRegistrationManifest(**(values | changes))


def test_c1_2_dispatch_window_constants_and_boundaries():
    assert nc.DISPATCH_TTL_MS == 1500
    assert nc.DISPATCH_WINDOW_RULES == {
        "dispatch_ttl_ms": 1500,
        "original_offer_deadline_immutable": True,
        "command_inherits_offer_deadline": True,
        "ready_write_transactions": 1,
        "post_consumed_fsync_expiry_check": True,
        "refresh_or_reissue": False,
    }
    # Exactly the cap is a valid encoded lifetime; anything larger is not.
    at_cap = dispatch(issued_at=NOW, expires_at=NOW + timedelta(milliseconds=1500))
    assert at_cap.expires_at - at_cap.issued_at == timedelta(milliseconds=1500)
    with pytest.raises(v1.ContractError):
        dispatch(issued_at=NOW, expires_at=NOW + timedelta(milliseconds=1500.001))
    # The historical 500 ms boundary stays valid inside the wider cap.
    dispatch(issued_at=NOW, expires_at=NOW + timedelta(milliseconds=500))
    dispatch(issued_at=NOW, expires_at=NOW + timedelta(milliseconds=1499.999))


def test_c1_2_offer_deadline_is_one_immutable_budget():
    deadlines = (NOW + timedelta(milliseconds=900), NOW + timedelta(seconds=30))
    assert nc.compute_offer_deadline(NOW, deadlines) == NOW + timedelta(milliseconds=900)
    far_only = (NOW + timedelta(seconds=30),)
    assert nc.compute_offer_deadline(NOW, far_only) == NOW + timedelta(milliseconds=1500)
    with pytest.raises(v1.ContractError):
        nc.compute_offer_deadline(NOW, ())
    with pytest.raises(v1.ContractError):
        nc.compute_offer_deadline(NOW.replace(tzinfo=None), far_only)


def test_c1_2_command_inherits_offer_deadline_without_renewal():
    offer_deadline = NOW + timedelta(milliseconds=700)
    late_issue = NOW + timedelta(milliseconds=500)
    # /ready cannot grant a fresh 1500 ms: the offer deadline binds.
    deadline = nc.compute_command_deadline(offer_deadline, late_issue,
                                           (NOW + timedelta(seconds=30),))
    assert deadline == offer_deadline
    early_issue = NOW
    assert nc.compute_command_deadline(offer_deadline, early_issue,
                                       (NOW + timedelta(seconds=30),)) == offer_deadline
    # A tighter current deadline wins; the TTL cap always applies.
    assert nc.compute_command_deadline(offer_deadline, early_issue,
                                       (NOW + timedelta(milliseconds=300),)) == \
        NOW + timedelta(milliseconds=300)
    assert nc.compute_command_deadline(NOW + timedelta(seconds=30), early_issue,
                                       (NOW + timedelta(seconds=30),)) == \
        NOW + timedelta(milliseconds=1500)
    with pytest.raises(v1.ContractError):
        nc.compute_command_deadline(offer_deadline, late_issue, ())


def test_c1_2_admission_requires_open_window_and_offer_inheritance():
    offer_deadline = NOW + timedelta(milliseconds=1500)
    command = dispatch(issued_at=NOW, expires_at=NOW + timedelta(milliseconds=1500))
    assert nc.validate_dispatch_admission(command, offer_deadline, now=NOW).ok
    assert nc.validate_dispatch_admission(
        command, offer_deadline, now=NOW + timedelta(milliseconds=1499.999)).ok
    expired = nc.validate_dispatch_admission(
        command, offer_deadline, now=NOW + timedelta(milliseconds=1500))
    assert "EXPIRED" in expired.blockers  # equality with expiry is expired
    before = nc.validate_dispatch_admission(command, offer_deadline,
                                            now=NOW - timedelta(milliseconds=1))
    assert "NOT_YET_ISSUED" in before.blockers
    outlives = nc.validate_dispatch_admission(command, offer_deadline,
                                              now=NOW)
    assert outlives.ok
    shorter_offer = NOW + timedelta(milliseconds=1000)
    assert "COMMAND_OUTLIVES_OFFER" in nc.validate_dispatch_admission(
        command, shorter_offer, now=NOW).blockers
    with pytest.raises(v1.ContractError):
        nc.validate_dispatch_admission(command, shorter_offer, now=NOW.replace(tzinfo=None))


def test_c1_3_deadline_result_consistency_and_fail_closed_bases():
    ok_result = ne.EvidenceDeadlineResult(
        validation=v1.ValidationResult(blockers=()), deadline=NOW + timedelta(milliseconds=1))
    assert ok_result.validation.ok and ok_result.deadline is not None
    with pytest.raises(v1.ContractError):  # ok without a deadline is contradictory
        ne.EvidenceDeadlineResult(validation=v1.ValidationResult(blockers=()),
                                  deadline=None)
    with pytest.raises(v1.ContractError):  # rejected result cannot carry a deadline
        ne.EvidenceDeadlineResult(
            validation=v1.ValidationResult(blockers=("NATIVE_PERMIT_EXPIRED",)),
            deadline=NOW + timedelta(milliseconds=1))
    with pytest.raises(v1.ContractError):
        ne.EvidenceDeadlineResult(validation=v1.ValidationResult(blockers=()),
                                  deadline=NOW.replace(tzinfo=None))
    base = ne.EvidenceRegistry().applicable_deadline(
        "SIM_PERMIT_1", action=v1.AuthorizationAction.OPEN_LONG, now=NOW)
    assert base.validation.blockers == ("NATIVE_DEADLINE_UNAVAILABLE",) and base.deadline is None
    production = ne.ProductionEvidenceRegistry().applicable_deadline(
        "SIM_PERMIT_1", action=v1.AuthorizationAction.CANCEL_OWNED, now=NOW)
    assert production.validation.blockers == ("NATIVE_PERMIT_UNREGISTERED",)
    with pytest.raises(v1.ContractError):
        ne.EvidenceRegistry().applicable_deadline(
            "SIM_PERMIT_1", action=v1.AuthorizationAction.OPEN_LONG, now=NOW.replace(tzinfo=None))


class SyntheticDeadlineRegistry(ne.EvidenceRegistry):
    """RD-03 formula reference implementation for SYNTHETIC test evidence."""

    def __init__(self, permits, authorization_deadlines, baselines, routes, cash_deadlines):
        self._permits = permits
        self._authorization_deadlines = authorization_deadlines
        self._baselines = baselines
        self._routes = routes
        self._cash = cash_deadlines

    def applicable_deadline(self, permit_id, *, action, now):
        validate_utc = ne.validate_utc_datetime
        validate_utc(now, "now")
        permit = self._permits.get(permit_id)
        if permit is None:
            return ne.EvidenceDeadlineResult(
                validation=v1.ValidationResult(blockers=("NATIVE_PERMIT_UNREGISTERED",)),
                deadline=None)
        if action not in permit.actions:
            return ne.EvidenceDeadlineResult(
                validation=v1.ValidationResult(blockers=("AUTHORIZATION_ACTION",)),
                deadline=None)
        if permit.expires_at <= now:
            return ne.EvidenceDeadlineResult(
                validation=v1.ValidationResult(blockers=("NATIVE_PERMIT_EXPIRED",)),
                deadline=None)
        bounds = [permit.expires_at, self._authorization_deadlines[permit_id]]
        if action is v1.AuthorizationAction.CANCEL_OWNED:
            bounds.append(self._routes[permit_id].expires_at)
        else:
            baseline = self._baselines.get(permit_id)
            if baseline is None or self._cash.get(permit_id) is None:
                return ne.EvidenceDeadlineResult(
                    validation=v1.ValidationResult(blockers=("NATIVE_DEADLINE_UNAVAILABLE",)),
                    deadline=None)
            bounds.append(baseline.expires_at)
            bounds.append(self._routes[permit_id].expires_at)
            bounds.append(self._cash[permit_id])
        deadline = min(bounds)
        if deadline <= now:
            return ne.EvidenceDeadlineResult(
                validation=v1.ValidationResult(blockers=("NATIVE_DEADLINE_EXPIRED",)),
                deadline=None)
        return ne.EvidenceDeadlineResult(validation=v1.ValidationResult(blockers=()),
                                         deadline=deadline)


def deadline_registry(**overrides):
    later = NOW + timedelta(seconds=30)
    permits = {p.permit_id: p for p in (
        permit(actions=(v1.AuthorizationAction.OPEN_LONG, v1.AuthorizationAction.CANCEL_OWNED),
               expires_at=overrides.get("permit_expires", NOW + timedelta(milliseconds=700))),)}
    return SyntheticDeadlineRegistry(
        permits=permits,
        authorization_deadlines={"SIM_PERMIT_1": overrides.get("authorization_expires",
                                                               NOW + timedelta(milliseconds=900))},
        baselines=overrides.get("baselines", {"SIM_PERMIT_1":
                                              baseline(expires_at=NOW + timedelta(milliseconds=300))}),
        routes=overrides.get("routes", {"SIM_PERMIT_1":
                                        route_evidence(expires_at=NOW + timedelta(milliseconds=500))}),
        cash_deadlines=overrides.get("cash", {"SIM_PERMIT_1":
                                              NOW + timedelta(milliseconds=400)}))


def test_c1_3_registry_applicability_rules_and_golden_vector():
    registry = deadline_registry()
    submit = registry.applicable_deadline("SIM_PERMIT_1",
                                          action=v1.AuthorizationAction.OPEN_LONG, now=NOW)
    assert submit.deadline == NOW + timedelta(milliseconds=300)  # baseline is tightest for SUBMIT
    cancel = registry.applicable_deadline("SIM_PERMIT_1",
                                          action=v1.AuthorizationAction.CANCEL_OWNED, now=NOW)
    assert cancel.deadline == NOW + timedelta(milliseconds=500)  # route binds CANCEL, not baseline/cash
    # Each earlier bound wins independently.
    assert deadline_registry(permit_expires=NOW + timedelta(milliseconds=120)).applicable_deadline(
        "SIM_PERMIT_1", action=v1.AuthorizationAction.OPEN_LONG, now=NOW).deadline == \
        NOW + timedelta(milliseconds=120)
    # Missing SUBMIT-only cash deadline is unavailable, never the permit lifetime.
    unavailable = deadline_registry(cash={"SIM_PERMIT_1": None}).applicable_deadline(
        "SIM_PERMIT_1", action=v1.AuthorizationAction.OPEN_LONG, now=NOW)
    assert unavailable.validation.blockers == ("NATIVE_DEADLINE_UNAVAILABLE",)
    assert unavailable.deadline is None
    # Equality with an applicable dependency deadline blocks.
    expired = deadline_registry(routes={"SIM_PERMIT_1":
                                        route_evidence(expires_at=NOW)}).applicable_deadline(
        "SIM_PERMIT_1", action=v1.AuthorizationAction.CANCEL_OWNED, now=NOW)
    assert expired.validation.blockers == ("NATIVE_DEADLINE_EXPIRED",)
    # Unauthorized CANCEL action.
    denied = deadline_registry().applicable_deadline(
        "SIM_PERMIT_1", action=v1.AuthorizationAction.REVERSE_TO_LONG, now=NOW)
    assert denied.validation.blockers == ("AUTHORIZATION_ACTION",)
    # Permit beyond its own expiry.
    late = deadline_registry().applicable_deadline(
        "SIM_PERMIT_1", action=v1.AuthorizationAction.OPEN_LONG,
        now=NOW + timedelta(milliseconds=700))
    assert late.validation.blockers == ("NATIVE_PERMIT_EXPIRED",)
    # Unknown permit id.
    unknown = deadline_registry().applicable_deadline(
        "SIM_PERMIT_X", action=v1.AuthorizationAction.OPEN_LONG, now=NOW)
    assert unknown.validation.blockers == ("NATIVE_PERMIT_UNREGISTERED",)


def test_c1_3_fixture_records_deadline_and_cancel_d01_vectors():
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    deadlines = doc["vectors"]["registry_deadlines"]
    assert deadlines["expected_submit_deadline_ms"] == 300
    assert deadlines["expected_cancel_deadline_ms"] == 500
    cancel_d01 = doc["vectors"]["cancel_d01"]
    assert cancel_d01["excludes"] == ["policy.valid_until",
                                      "quote/account/positions/contract/packet TTLs",
                                      "submit_readiness/full_account_proof",
                                      "baseline_and_cash_freshness(submit_only)"]
    assert cancel_d01["includes"] == ["intent.expires_at",
                                      "context.authorization.expires_at(CANCEL_OWNED)",
                                      "D_registry(CANCEL_OWNED)", "D_route"]


def offer_record(kind=nc.CommandKind.SUBMIT, **changes):
    values = dict(offer=offer(), first_issued_at=NOW,
                  kind=kind,
                  cancel_id="SIM_CANCEL_1" if kind is nc.CommandKind.CANCEL else None)
    return nc.OfferDeadlineRecord(**(values | changes))


def test_c1_4_offer_deadline_record_is_local_only_and_immutable():
    record = offer_record()
    wire = nc.encode_local(record)
    assert nc.decode_local(wire, nc.OfferDeadlineRecord) == record
    assert wire == nc.encode_local(record)          # stable canonical bytes
    assert json.loads(wire)["schema_version"] == nc.LOCAL_SCHEMA_VERSION
    with pytest.raises(v1.ContractError):           # transport decoder refuses it
        nc.decode(wire, nc.OfferDeadlineRecord)
    with pytest.raises(v1.ContractError):           # v1 decoder refuses it
        v1.decode(wire, nc.OfferDeadlineRecord)
    cancelled = offer_record(kind=nc.CommandKind.CANCEL)
    assert nc.decode_local(nc.encode_local(cancelled), nc.OfferDeadlineRecord) == cancelled
    assert cancelled.cancel_id == "SIM_CANCEL_1"


def test_c1_4_offer_record_lifetime_boundaries():
    exact = offer_record(offer=offer(expires_at=NOW + timedelta(milliseconds=1500)))
    assert exact.offer.expires_at == NOW + timedelta(milliseconds=1500)
    with pytest.raises(v1.ContractError):  # over the frozen budget
        offer_record(offer=offer(expires_at=NOW + timedelta(milliseconds=1500.001)))
    with pytest.raises(v1.ContractError):  # zero/negative lifetime
        offer_record(offer=offer(expires_at=NOW))
    with pytest.raises(v1.ContractError):  # expiry before issuance
        offer_record(offer=offer(expires_at=NOW - timedelta(milliseconds=1)))
    with pytest.raises(v1.ContractError):  # naive first timestamp
        offer_record(first_issued_at=NOW.replace(tzinfo=None))


def test_c1_4_kind_specific_cancel_identity_and_diagnostics_frozen():
    with pytest.raises(v1.ContractError):  # SUBMIT cannot carry a cancel identity
        offer_record(cancel_id="SIM_CANCEL_1")
    with pytest.raises(v1.ContractError):  # owned CANCEL requires its identity
        offer_record(kind=nc.CommandKind.CANCEL, cancel_id=None)
    assert nc.OFFER_DEADLINE_BLOCKERS == frozenset({
        "OFFER_DEADLINE_REQUIRED", "OFFER_DEADLINE_CONFLICT",
        "OFFER_DEADLINE_UNPROVEN", "OFFER_DEADLINE_CLOCK_INVALID"})
    assert nc.LOCAL_SCHEMA_VERSION == "infiny_execution_local_v1"


def test_c1_4_fixture_records_r3_p14_and_zero_drift_vectors():
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    offer_fd = doc["vectors"]["offer_deadline"]
    assert offer_fd["option"] == "A_durable_first_offer"
    assert offer_fd["r3_p14"]["first_issued_at"] == "2026-09-14T01:29:59Z"
    assert offer_fd["r3_p14"]["original_expiry_ms"] == 1500
    assert offer_fd["r3_p14"]["recovery_offsets_ms"] == [400, 1400, 1500, 1600, 2100, 3000]
    assert offer_fd["r3_p14"]["zero_drift_rule"] == \
        "recovered offer bytes and first_issued_at identical; no usable offer at/after original deadline"
    assert offer_fd["banned_expression"] == "E(r) = min(s, T + r) + 1500 ms"
    assert set(offer_fd["blockers"]) == set(nc.OFFER_DEADLINE_BLOCKERS)
    assert offer_fd["precedence"] == ["structural_type_error", "nonpositive_lifetime",
                                      "lifetime_over_budget", "kind_cancel_identity"]
    names = {case["name"] for case in doc["cases"]}
    assert "offer_deadline_record_synthetic" in names


def test_c1_4_frozen_precedence_for_multiply_invalid_records():
    """F2 closure: precedence for simultaneously invalid inputs is structural
    type errors first (engine ContractError), then nonpositive lifetime, then
    over-budget lifetime, then kind/cancel identity. Frozen in the fixture."""
    # nonpositive wins over a missing CANCEL identity
    with pytest.raises(v1.ContractError, match="strictly after first issuance"):
        offer_record(kind=nc.CommandKind.CANCEL, cancel_id=None,
                     offer=offer(expires_at=NOW))
    # over-budget wins over a SUBMIT cancel identity
    with pytest.raises(v1.ContractError, match="exceeds the frozen 1500 ms"):
        offer_record(cancel_id="SIM_CANCEL_1",
                     offer=offer(expires_at=NOW + timedelta(milliseconds=1500.001)))
    # structural type error wins over every semantic rule
    with pytest.raises(v1.ContractError):
        offer_record(first_issued_at=NOW.replace(tzinfo=None),
                     kind=nc.CommandKind.CANCEL, cancel_id=None,
                     offer=offer(expires_at=NOW))
    # v2 records keep their namespace-dispatched encoding path (c1.4-r2/F1)
    control = nc.ControlState(control_revision=0, binding=binding(),
                              mode=v1.ExecutionMode.DISABLED, policy_id="SIM_POLICY_1",
                              authorization_id=None, native_permit_id=None,
                              params_sha256="0" * 64, generation="SIM_GEN_1",
                              updated_at=NOW, disabled_reason=None)
    assert nc.decode(nc.encode(control), nc.ControlState) == control


def test_c1_5_submit_first_deadline_formula_is_frozen():
    """ANC-T01: E_first_SUBMIT = min(D_S, f + tau, s + tau); the C1.5 remedy
    consolidates preparation transactions and never relaxes the sample cap."""
    tau = timedelta(milliseconds=1500)
    base = NOW
    # Analytical counterexample: s=T, f=T+1154ms, D_S=T+3000ms -> frozen 1500ms.
    sample, first = base, base + timedelta(milliseconds=1154)
    d_original = (base + timedelta(milliseconds=3000),)
    frozen = nc.compute_offer_deadline(first, d_original + (sample + tau,))
    assert frozen == base + timedelta(milliseconds=1500)
    assert frozen != first + tau  # issuance-only (T+2654ms) is not the contract
    # R3-P15 old sample: s = f - 500ms -> f + 1000ms.
    old_sample = first - timedelta(milliseconds=500)
    assert nc.compute_offer_deadline(first, d_original + (old_sample + tau,)) == \
        first + timedelta(milliseconds=1000)
    # s == f takes the full issuance ceiling when other bounds are later.
    assert nc.compute_offer_deadline(first, d_original + (first + tau,)) == first + tau
    tight = (base + timedelta(milliseconds=900),)
    assert nc.compute_offer_deadline(first, tight + (first + tau,)) == \
        base + timedelta(milliseconds=900)
    # CANCEL has no sample/packet cap (CANCEL-D-01 bounds only).
    assert nc.compute_offer_deadline(first, d_original) == first + tau


def test_c1_5_preparation_seam_and_write_counts_frozen():
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    seam = doc["vectors"]["preparation_seam"]
    assert seam["option"] == "b_preparation_transaction_consolidation"
    assert seam["returns"] == "(reserved original Plan, final persisted ExecutionContext)"
    assert seam["signatures"]["DispatchJournal.prepare_native_submission"] == \
        "(intent, context, *, expected_control_revision, expected_context, resources=())"
    assert seam["signatures"]["Ledger.prepare_native_submission"] == \
        "(intent, context, *, expected_control_revision, expected_context, " \
        "resources=(), evidence_registry=None)"
    counts = seam["write_counts"]
    assert counts["preparation_writes"] == {"current": 6, "ordered": 1}
    assert counts["identified_first_offer_writes"] == {"current": 8, "ordered": 3}
    assert counts["ready_dispatch_publication_writes"] == 1
    assert seam["unchanged"] == ["DISPATCH_TTL_MS=1500", "E_first formulas",
                                 "record_offer/get_offer/begin_dispatch signatures",
                                 "CANCEL path (not through the SUBMIT seam)"]
    assert seam["anc_t01"]["analytical_case"]["skew_ms"] == 1154
    assert seam["anc_t01"]["analytical_case"]["frozen_deadline_ms"] == 1500
    assert seam["anc_t01"]["analytical_case"]["issuance_only_deadline_ms"] == 2654


def test_operator_manifest_is_explicit_local_only():
    item = manifest()
    assert nc.decode(nc.encode(item), ne.OperatorRegistrationManifest) == item
    assert ne.OPERATOR_REGISTRATION_SCHEMA_VERSION == "infiny_native_operator_registration_v1"
    for bad in (
        dict(record_path="https://sim.example/permit.json"),
        dict(record_path=r"\\server\share\permit.json"),
        dict(authorization_artifact_path=r"C:\sim\%TOKEN%\authz.txt"),
        dict(record_path=r"C:\sim\tool.exe"),
        dict(artifacts=()),
    ):
        with pytest.raises(v1.ContractError):
            manifest(**bad)


def test_synthetic_fixture_cases_roundtrip():
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
    assert cases
    by_name = {case["name"]: case for case in cases}
    for name in ("offer_submit", "dispatch_cancel", "receipt_no_call",
                 "permit_synthetic", "baseline_synthetic", "poll_request_first",
                 "poll_request_snapshot_by_value", "poll_response_offer",
                 "ready_response_dispatch", "results_response_persisted",
                 "events_response_contiguous_ack", "control_response_recorded",
                 "status_response_readonly", "transport_error_poll_too_fast",
                 "event_cursor_positive", "operator_manifest_synthetic",
                 "dispatch_command_full_window"):
        assert name in by_name
    for case in cases:
        namespace = case["schema_namespace"]
        expected = {
            "transport": nc.TRANSPORT_SCHEMA_VERSION,
            "v2": v1.V2_SCHEMA_VERSION,
            "local": nc.LOCAL_SCHEMA_VERSION,
        }[namespace]
        wire = json.dumps({"schema_version": expected, "type": case["type"], "data": case["data"]},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        module = nc if namespace in ("transport", "local") else ne
        if namespace == "local":
            decoded = nc.decode_local(wire, getattr(module, case["type"]))
            reencoded = nc.encode_local(decoded)
        else:
            decoded = nc.decode(wire, getattr(module, case["type"]))
            reencoded = nc.encode(decoded)
        assert reencoded == wire
