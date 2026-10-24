"""Probability sub-package version marker for node M3A (devplan-v2.1.0).

This package intentionally does not re-export symbols: downstream nodes must
import from ``ak_trading.v2.probability.<module>`` directly, so integration
never has to touch this shared file (开发总纲 §3).
"""
from __future__ import annotations

NODE_VERSION = "M3A-v2.1.0"
LABEL_VERSION = "path-label-v2.1.0"
FORECAST_FIT_VERSION = "forecast-fit-v2.1.0"
MODELS_SCHEMA_VERSION = "ak_probability_models_v2.1.0"
REGIME_REFERENCE_SCHEMA_VERSION = "ak_probability_regime_reference_v2.1.0"
REQUESTS_SCHEMA_VERSION = "ak_probability_requests_v2.1.0"
LABELS_SCHEMA_VERSION = "ak_probability_labels_v2.1.0"
LABELS_CSV_SCHEMA_VERSION = "ak_probability_labels_v2.1.0"
FORECASTS_SCHEMA_VERSION = "ak_probability_forecasts_v2.1.0"
REGIMES_SCHEMA_VERSION = "ak_probability_regimes_v2.1.0"
CALIBRATION_SCHEMA_VERSION = "ak_probability_calibration_v2.1.0"
MODEL_DIAGNOSTICS_SCHEMA_VERSION = "ak_probability_model_diagnostics_v2.1.0"
MODEL_RUN_SCHEMA_VERSION = "ak_probability_model_run_v2.1.0"
