"""
Tests for BillingService.

Sections:
  1. Payload shape — _build_event produces exactly what Stripe expects
  2. MOCK mode     — prints to stdout, never calls Stripe
  3. LIVE mode     — calls stripe.billing.MeterEvent.create with correct args
  4. Graceful degradation — failures never propagate; warnings are emitted
  5. Zero-code-change guarantee — same payload in both modes
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from billing import BillingService


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_service() -> BillingService:
    """BillingService in MOCK mode (no Stripe key)."""
    return BillingService(api_key=None, meter_event_name="test_event")


@pytest.fixture
def live_service() -> BillingService:
    """BillingService in LIVE mode with a fake key."""
    return BillingService(api_key="sk_test_fake", meter_event_name="test_event")


# ── 1. Payload shape ──────────────────────────────────────────────────────────

def test_build_event_has_required_stripe_fields(mock_service):
    """_build_event must include all fields required by the Stripe MeterEvent API."""
    event = mock_service._build_event(api_key_id="cus_abc123")

    assert "event_name" in event
    assert "payload" in event
    assert "timestamp" in event


def test_build_event_stripe_customer_id(mock_service):
    event = mock_service._build_event(api_key_id="cus_abc123")
    assert event["payload"]["stripe_customer_id"] == "cus_abc123"


def test_build_event_value_is_string_not_int(mock_service):
    """Stripe's API requires value as a string — '1', never 1."""
    event = mock_service._build_event(api_key_id="cus_abc123")
    assert isinstance(event["payload"]["value"], str), (
        "Stripe MeterEvent payload.value must be a string"
    )
    assert event["payload"]["value"] == "1"


def test_build_event_uses_configured_meter_name(mock_service):
    event = mock_service._build_event(api_key_id="cus_abc123")
    assert event["event_name"] == "test_event"


def test_build_event_timestamp_is_recent_unix_epoch(mock_service):
    before = int(time.time())
    event = mock_service._build_event(api_key_id="cus_abc123")
    after = int(time.time()) + 1
    assert isinstance(event["timestamp"], int)
    assert before <= event["timestamp"] <= after


# ── 2. MOCK mode ──────────────────────────────────────────────────────────────

def test_mock_mode_is_active_when_no_key():
    svc = BillingService(api_key=None)
    assert svc.mock_mode is True


def test_mock_mode_prints_to_stdout(mock_service, capsys):
    mock_service.record_invocation(api_key_id="cus_abc", tool_name="validate_json")
    out = capsys.readouterr().out
    assert "[BillingService MOCK]" in out
    assert "validate_json" in out


def test_mock_mode_output_contains_valid_json(mock_service, capsys):
    """The printed event payload must itself be valid JSON."""
    mock_service.record_invocation(api_key_id="cus_abc", tool_name="repair_json")
    out = capsys.readouterr().out
    # Extract the JSON blob after 'event='
    json_part = out.split("event=", 1)[1].strip()
    parsed = json.loads(json_part)  # raises if invalid
    assert parsed["payload"]["stripe_customer_id"] == "cus_abc"


def test_mock_mode_does_not_call_stripe(mock_service):
    with patch("stripe.billing.MeterEvent.create") as mock_create:
        mock_service.record_invocation(api_key_id="cus_abc", tool_name="repair_json")
    mock_create.assert_not_called()


# ── 3. LIVE mode ──────────────────────────────────────────────────────────────

def test_live_mode_is_active_when_key_present():
    svc = BillingService(api_key="sk_test_fake")
    assert svc.mock_mode is False


def test_live_mode_calls_stripe_meter_event(live_service):
    with patch("stripe.billing.MeterEvent.create") as mock_create:
        live_service.record_invocation(api_key_id="cus_xyz", tool_name="sanitize_json_output")

    mock_create.assert_called_once()
    kwargs = mock_create.call_args.kwargs
    assert kwargs["event_name"] == "test_event"
    assert kwargs["payload"]["stripe_customer_id"] == "cus_xyz"
    assert kwargs["payload"]["value"] == "1"
    assert isinstance(kwargs["timestamp"], int)


def test_live_mode_does_not_print(live_service, capsys):
    with patch("stripe.billing.MeterEvent.create"):
        live_service.record_invocation(api_key_id="cus_xyz", tool_name="repair_json")
    assert capsys.readouterr().out == ""


# ── 4. Graceful degradation ───────────────────────────────────────────────────

def test_no_billing_when_api_key_id_is_none(live_service):
    with patch("stripe.billing.MeterEvent.create") as mock_create:
        live_service.record_invocation(api_key_id=None, tool_name="validate_json")
    mock_create.assert_not_called()


def test_no_billing_when_api_key_id_is_empty_string(live_service):
    with patch("stripe.billing.MeterEvent.create") as mock_create:
        live_service.record_invocation(api_key_id="", tool_name="validate_json")
    mock_create.assert_not_called()


def test_stripe_error_does_not_propagate(live_service):
    with patch("stripe.billing.MeterEvent.create", side_effect=Exception("Stripe down")):
        live_service.record_invocation(api_key_id="cus_xyz", tool_name="repair_json")


def test_stripe_failure_emits_warning(live_service, caplog):
    import logging
    with patch("stripe.billing.MeterEvent.create", side_effect=RuntimeError("timeout")):
        with caplog.at_level(logging.WARNING, logger="billing"):
            live_service.record_invocation(api_key_id="cus_xyz", tool_name="repair_json")
    assert any("Stripe billing failed" in m for m in caplog.messages)


# ── 5. Zero-code-change guarantee ─────────────────────────────────────────────

def test_mock_and_live_produce_identical_payload_shape():
    """
    _build_event output must be byte-for-byte identical regardless of mode.
    This guarantees that switching from mock → live is purely a config change.
    """
    mock_svc = BillingService(api_key=None, meter_event_name="my_meter")
    live_svc = BillingService(api_key="sk_test_fake", meter_event_name="my_meter")

    # Freeze time so timestamps match
    frozen_ts = 1_700_000_000
    with patch("billing.time") as mock_time:
        mock_time.time.return_value = frozen_ts
        event_mock = mock_svc._build_event(api_key_id="cus_same")
        event_live = live_svc._build_event(api_key_id="cus_same")

    assert event_mock == event_live, (
        "Mock and live services must produce identical payloads — "
        "switching modes should require zero code changes"
    )
