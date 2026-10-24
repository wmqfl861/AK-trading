"""Version constants for the v2.1 probability/strategy contract namespace.

This package intentionally does NOT re-export submodules: downstream nodes
must import from ``ak_trading.v2.<module>`` directly so that later
integration work never has to touch this shared file (devplan-v2.1.0 §3).
"""
from __future__ import annotations

DOCUMENT_VERSION = "devplan-v2.1.7"
CONTRACT_VERSION = "ak_probability_strategy_contract_v2.1.3"
INPUT_SCHEMA_VERSION = "ak_probability_input_v2.1.0"
SHARED_FIXTURE_SCHEMA_VERSION = "ak_probability_shared_fixture_v2.1.0"
CONTRACT_LOCK_SCHEMA_VERSION = "ak_probability_contract_lock_v2.1.0"
M1_NODE_VERSION = "M1-v2.1.3"
