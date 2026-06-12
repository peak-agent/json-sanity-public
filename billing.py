"""
Stripe metered billing — reports every successful tool invocation.

Billing model: $1.00/mo base fee includes first 100 invocations; overages
billed at $0.01/invocation via Stripe tiered meter pricing. The code
always reports 100% of usage — Stripe's billing engine applies the tier
so users are never double-charged for the first 100.

BillingService operates in two modes determined at instantiation:
  - LIVE mode  (STRIPE_SECRET_KEY is set): sends MeterEvents to Stripe
  - MOCK mode  (key absent):               prints the event to stdout

The payload produced is identical in both modes, so swapping in a real
key is a zero-code change — just set the env var.

Env vars:
  STRIPE_SECRET_KEY        – sk_live_... or sk_test_...
  STRIPE_METER_EVENT_NAME  – meter event name in Stripe dashboard
                             (default: "json_sanity_tool_invocations")
                             Must match the meter name configured in your
                             Stripe dashboard exactly.
"""

from __future__ import annotations

import json
import logging
import os
import time

import stripe

logger = logging.getLogger(__name__)


class BillingService:
    """
    Builds and dispatches Stripe MeterEvents for each successful tool call.

    In MOCK mode the event is printed to stdout instead of sent to Stripe,
    giving developers a live preview of exactly what Stripe would receive.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        meter_event_name: str = "json_sanity_tool_invocations",
    ) -> None:
        self.meter_event_name = meter_event_name
        self.mock_mode = not api_key
        if not self.mock_mode:
            stripe.api_key = api_key

    # ── public ────────────────────────────────────────────────────────────────

    def record_invocation(self, *, api_key_id: str | None, tool_name: str) -> None:
        """
        Record one billable unit for the customer identified by api_key_id.

        Skips silently when api_key_id is falsy (anonymous caller).
        Stripe failures are caught, logged as WARNING, and never re-raised.
        """
        if not api_key_id:
            return

        event = self._build_event(api_key_id=api_key_id)

        if self.mock_mode:
            print(
                f"[BillingService MOCK] tool={tool_name!r} "
                f"event={json.dumps(event)}"
            )
            return

        try:
            stripe.billing.MeterEvent.create(**event)
            logger.debug("Billed 1 unit to %s for tool %r", api_key_id, tool_name)
        except Exception as exc:
            logger.warning(
                "Stripe billing failed for %s / %s: %s", api_key_id, tool_name, exc
            )

    # ── internal ──────────────────────────────────────────────────────────────

    def _build_event(self, *, api_key_id: str) -> dict:
        """
        Construct the MeterEvent kwargs dict.

        This method is the single source of truth for payload shape —
        both mock and live paths use it, so the format is always identical.
        """
        return {
            "event_name": self.meter_event_name,
            "payload": {
                "stripe_customer_id": api_key_id,
                "value": "1",
            },
            "timestamp": int(time.time()),
        }


# Module-level singleton — picks up env vars at import time.
# server.py imports this instance directly.
billing_service = BillingService(
    api_key=os.environ.get("STRIPE_SECRET_KEY"),
    meter_event_name=os.environ.get(
        "STRIPE_METER_EVENT_NAME", "json_sanity_tool_invocations"
    ),
)
