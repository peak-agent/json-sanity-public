"""
Integration tests for graceful degradation when Supabase is unreachable.
All tests mock the Supabase client so no real network calls are made.
"""

from __future__ import annotations

import importlib
import logging
from unittest.mock import MagicMock, patch

import pytest

import db
from repair_logic import sanitize_json_output


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_broken_client(exc: Exception) -> MagicMock:
    """Return a mock Supabase client whose .insert().execute() raises exc."""
    mock_execute = MagicMock(side_effect=exc)
    mock_insert = MagicMock()
    mock_insert.execute = mock_execute
    mock_table = MagicMock()
    mock_table.insert.return_value = mock_insert
    client = MagicMock()
    client.table.return_value = mock_table
    return client


def _make_healthy_client() -> MagicMock:
    """Return a mock Supabase client whose .insert().execute() succeeds."""
    mock_execute = MagicMock(return_value={"data": [{"id": 1}], "error": None})
    mock_insert = MagicMock()
    mock_insert.execute = mock_execute
    mock_table = MagicMock()
    mock_table.insert.return_value = mock_insert
    client = MagicMock()
    client.table.return_value = mock_table
    return client


# ── graceful degradation ──────────────────────────────────────────────────────

def test_tool_succeeds_when_db_is_unreachable():
    """sanitize_json_output must return valid JSON even if Supabase is down."""
    broken = _make_broken_client(ConnectionError("Supabase unreachable"))
    with patch.object(db, "get_client", return_value=broken):
        # Valid JSON — no repair needed, but logging will fail
        sanitized, fixes = sanitize_json_output('{"key": "value"}')
    assert sanitized == '{"key": "value"}'
    assert fixes == []


def test_tool_repairs_and_succeeds_when_db_is_unreachable():
    """Repair logic must complete and return even when the log write fails."""
    broken = _make_broken_client(TimeoutError("DB timeout"))
    with patch.object(db, "get_client", return_value=broken):
        sanitized, fixes = sanitize_json_output(
            'Here is your data: {"name": "Alice"}'
        )
    import json
    assert json.loads(sanitized) == {"name": "Alice"}
    assert any("preamble" in f for f in fixes)


def test_db_failure_emits_warning(caplog):
    """A Supabase failure must log a warning, not raise."""
    broken = _make_broken_client(OSError("network error"))
    with patch.object(db, "get_client", return_value=broken):
        with caplog.at_level(logging.WARNING, logger="db"):
            db.log_sanitize_call(
                input_length=42,
                repair_performed=True,
                api_key_id="test-key",
            )
    assert any("Failed to write sanitize log" in m for m in caplog.messages)


def test_db_exception_does_not_propagate():
    """log_sanitize_call must never raise, regardless of exception type."""
    for exc in (RuntimeError("boom"), ValueError("bad"), Exception("generic")):
        broken = _make_broken_client(exc)
        with patch.object(db, "get_client", return_value=broken):
            # Must not raise
            db.log_sanitize_call(
                input_length=10,
                repair_performed=False,
                api_key_id=None,
            )


# ── healthy path ──────────────────────────────────────────────────────────────

def test_log_inserts_correct_payload_when_db_healthy():
    """When the DB is reachable, verify the exact payload sent to Supabase."""
    healthy = _make_healthy_client()
    with patch.object(db, "get_client", return_value=healthy):
        db.log_sanitize_call(
            input_length=128,
            repair_performed=True,
            api_key_id="dev-abc123",
        )
    healthy.table.assert_called_once_with("sanitize_logs")
    healthy.table().insert.assert_called_once_with(
        {
            "input_length": 128,
            "repair_performed": True,
            "api_key_id": "dev-abc123",
        }
    )
    healthy.table().insert().execute.assert_called_once()


def test_log_accepts_null_api_key_id():
    """api_key_id=None is valid — anonymous callers must still be logged."""
    healthy = _make_healthy_client()
    with patch.object(db, "get_client", return_value=healthy):
        db.log_sanitize_call(
            input_length=5,
            repair_performed=False,
            api_key_id=None,
        )
    payload = healthy.table().insert.call_args[0][0]
    assert payload["api_key_id"] is None
