# encoding: UTF-8
"""Read-only history depth probe via native MarketCenter (no orders).

Loads without pythongo.ui/base: only INFINIGO + pythongo.core, avoiding the
NumPy/PyQt import chain that previously failed inside the embedded interpreter.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from urllib.request import Request, urlopen

import INFINIGO as _native


class Field:
    def __init__(self, default=None, *, title=""):
        self.default, self.title = default, title

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, instance, owner=None):
        return self if instance is None else instance.__dict__.get(self.name, self.default)

    def __set__(self, instance, value):
        if not isinstance(value, str):
            raise ValueError("probe parameters must be strings")
        instance.__dict__[self.name] = value.strip()


class Params:
    @classmethod
    def fields(cls):
        result = {}
        for parent in reversed(cls.__mro__):
            result.update({k: v for k, v in vars(parent).items() if isinstance(v, Field)})
        return result

    def model_dump(self):
        return {k: getattr(self, k) for k in self.fields()}

    def __iter__(self):
        return iter(self.model_dump().items())


class State(Params):
    pass


class BaseStrategy:
    def __init__(self):
        self.strategy_id = 0
        self.strategy_name = ""
        self.trading = False

    def get_params(self):
        return [{"name": k, "value": str(getattr(self.params_map, k)), "cn_name": f.title}
                for k, f in self.params_map.fields().items()]

    def set_params(self, data):
        for k, v in data.items():
            if k not in self.params_map.fields():
                raise ValueError("unknown parameter")
            setattr(self.params_map, k, v)

    def update_status_bar(self):
        _native.updateState(self.strategy_id, {
            f.title: str(getattr(self.state_map, k)) for k, f in self.state_map.fields().items()})

    def output(self, *messages):
        _native.writeLog(msg="[" + self.strategy_name + "] " + " ".join(map(str, messages)))


class ParamsModel(Params):
    exchange: str = Field(default="SHFE", title="交易所代码")
    instrument_id: str = Field(default="rb2610", title="合约代码")
    api_url: str = Field(default="http://127.0.0.1:17778/analyze", title="桥接服务地址")


class StateModel(State):
    probe_status: str = Field(default="pending", title="探测状态")
    last_result: str = Field(default="", title="最近结果")


class HistoryDepthProbe(BaseStrategy):
    def __init__(self) -> None:
        super().__init__()
        self.params_map = ParamsModel()
        self.state_map = StateModel()

    def _post(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = Request(self.params_map.api_url, data=body,
                         headers={"Content-Type": "application/json",
                                  "X-Infiny-Source": "HistoryDepthProbe"}, method="POST")
        with urlopen(request, timeout=3) as response:
            self.output(f"bridge response {response.getcode()}")

    def on_init(self) -> None:
        self.trading = False
        self.update_status_bar()
        self.output("history depth probe loaded; read-only, no orders")

    def on_start(self) -> None:
        self.trading = False
        self.update_status_bar()
        self.output("starting read-only history depth probe")
        threading.Thread(target=self._probe, daemon=True).start()

    def _probe(self) -> None:
        from pythongo.core import MarketCenter
        center = MarketCenter()
        exchange = self.params_map.exchange
        instrument = self.params_map.instrument_id
        windows = [
            ("recent_1d", datetime(2026, 9, 9, 9, 0), datetime(2026, 9, 9, 15, 0)),
            ("gap_20180806", datetime(2018, 8, 6, 9, 0), datetime(2018, 8, 6, 15, 0)),
            ("deep_20160701", datetime(2016, 7, 1, 9, 0), datetime(2016, 7, 1, 15, 0)),
        ]
        results = []
        for name, start, end in windows:
            entry = {"window": name, "start": start.isoformat(), "end": end.isoformat(),
                     "rows": None, "first": None, "last": None, "error": None}
            try:
                data = center.get_kline_data(exchange=exchange, instrument_id=instrument,
                                             style="M1", start_time=start, end_time=end, simply=True)
                entry["rows"] = len(data) if isinstance(data, list) else None
                if isinstance(data, list) and data:
                    first, last = data[0], data[-1]
                    entry["first"] = {k: first.get(k) for k in ("datetime", "open", "close")} if isinstance(first, dict) else str(first)[:80]
                    entry["last"] = {k: last.get(k) for k in ("datetime", "open", "close")} if isinstance(last, dict) else str(last)[:80]
            except Exception as exc:
                entry["error"] = type(exc).__name__
            results.append(entry)
            self.state_map.probe_status = f"probed_{name}"
            self.state_map.last_result = f"{name}:rows={entry['rows']}"
            self.output(json.dumps(entry, ensure_ascii=False))
            self.update_status_bar()
        payload = {
            "schema_version": 2,
            "exchange": exchange,
            "instrument_id": instrument,
            "metadata": {"probe": "history_depth_v1", "windows": [w[0] for w in windows]},
            "history_probe": results,
            "ticks": [],
            "runtime": {"observer_only": True, "enable_trading": False},
        }
        try:
            self._post(payload)
            self.state_map.probe_status = "complete"
        except Exception as exc:
            self.state_map.probe_status = f"post_failed:{type(exc).__name__}"
            self.output(f"post failed {type(exc).__name__}; local log has full results")
        self.update_status_bar()

    def on_stop(self) -> None:
        self.trading = False
        self.output("history depth probe stopped")
