"""Tests for the read-only coordination readiness checker.

All register/table inputs are SYNTHETIC fixtures mirroring the error classes
of the 2026-09 case, per Codex review report r1 section C6: the checker must
report each contradiction and must never upgrade a preparation state into a
completed one.
"""
