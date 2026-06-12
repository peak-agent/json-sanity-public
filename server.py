"""
MCP JSON Sanity server — StreamableHTTP transport (Starlette/uvicorn).

Uses stateless=True so every request is handled independently — no
in-process session map is needed, which means the server works correctly
behind Railway's Fastly CDN regardless of replica count.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from contextvars import ContextVar

import httpx
import stripe
import uvicorn
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool, ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from billing import billing_service
from db import log_sanitize_call
from repair_logic import (
    clean_llm_markdown,
    repair_json,
    repair_string,
    sanitize_json_output,
    validate_json,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_request_api_key: ContextVar[str | None] = ContextVar("request_api_key", default=None)

# ── Subscription auth cache ───────────────────────────────────────────────────

_subscription_cache: dict[str, tuple[bool, float]] = {}
_CACHE_TTL = 60  # seconds


def _verify_subscription(customer_id: str) -> bool:
    """Return True if customer has an active Stripe subscription.

    Skipped in mock mode (no STRIPE_SECRET_KEY) so local dev works without
    a real Stripe account. Results are cached for 60 s to keep latency low.
    """
    if billing_service.mock_mode:
        return True

    now = time.monotonic()
    cached = _subscription_cache.get(customer_id)
    if cached and now < cached[1]:
        return cached[0]

    try:
        subs = stripe.Subscription.list(customer=customer_id, status="active", limit=1)
        result = bool(subs.data)
    except Exception as exc:
        logger.warning("Stripe subscription check failed for %s: %s", customer_id, exc)
        result = False

    _subscription_cache[customer_id] = (result, now + _CACHE_TTL)
    return result


# ── MCP server ────────────────────────────────────────────────────────────────

mcp = Server("json-sanity")

_API_KEY_PROP = {
    "api_key_id": {
        "type": "string",
        "description": "Your Stripe Customer ID, used for metered billing ($1.00/mo base includes first 100 requests; $0.01 each after).",
    }
}

TOOLS: list[Tool] = [
    Tool(
        name="validate_json",
        description="Check whether a JSON string is valid. Returns parsed object on success.",
        inputSchema={
            "type": "object",
            "properties": {
                "json_string": {"type": "string", "description": "The JSON text to validate."},
                **_API_KEY_PROP,
            },
            "required": ["json_string"],
        },
        annotations=ToolAnnotations(
            readOnlyHint=True,
            idempotentHint=True,
            destructiveHint=False,
            openWorldHint=False,
        ),
    ),
    Tool(
        name="repair_json",
        description=(
            "Attempt to repair common JSON issues: trailing commas, single quotes, "
            "unquoted keys, Python/JS literals, truncated structures."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "json_string": {"type": "string", "description": "The malformed JSON text to repair."},
                **_API_KEY_PROP,
            },
            "required": ["json_string"],
        },
        annotations=ToolAnnotations(
            readOnlyHint=True,
            idempotentHint=True,
            destructiveHint=False,
            openWorldHint=False,
        ),
    ),
    Tool(
        name="sanitize_json_output",
        description=(
            "Use this tool before saving any JSON data to session history or state files "
            "to prevent JSONDecodeErrors and session poisoning. It removes prose preambles "
            "and repairs malformed control characters."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "raw_string": {
                    "type": "string",
                    "description": "Raw string that should contain JSON, possibly with prose or control character issues.",
                },
                "api_key_id": {
                    "type": "string",
                    "description": "Your API key identifier, used to attribute crash-prevention metrics to your account.",
                },
            },
            "required": ["raw_string"],
        },
        annotations=ToolAnnotations(
            readOnlyHint=True,
            idempotentHint=True,
            destructiveHint=False,
            openWorldHint=False,
        ),
    ),
    Tool(
        name="repair_string",
        description=(
            "Deterministic repair engine. Given a raw LLM output that should contain "
            "JSON, this tool: (1) strips markdown code fences (```json), (2) regex-strips "
            "prose preambles/suffixes, (3) escapes unescaped control characters inside "
            "string values, (4) validates with json.loads — falling back to structural "
            "repairs and partial-recovery bracket closing when needed, and (5) optionally "
            "validates the repaired JSON against a JSON schema."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "raw_string": {
                    "type": "string",
                    "description": "Raw text that should contain JSON.",
                },
                "schema": {
                    "type": "object",
                    "description": (
                        "Optional JSON schema. When provided, the repaired JSON is "
                        "validated against it. Validation errors are translated into "
                        "a list of actionable 'Fix Action' strings."
                    ),
                },
                **_API_KEY_PROP,
            },
            "required": ["raw_string"],
        },
        annotations=ToolAnnotations(
            readOnlyHint=True,
            idempotentHint=True,
            destructiveHint=False,
            openWorldHint=False,
        ),
    ),
]


@mcp.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@mcp.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    api_key_id: str | None = arguments.get("api_key_id") or _request_api_key.get()

    # Auth gate — step 1: must look like a Stripe customer ID.
    if not api_key_id or not api_key_id.startswith("cus_"):
        return [TextContent(
            type="text",
            text=json.dumps({
                "error": "Unauthorized",
                "message": (
                    "api_key_id is required and must be a valid Stripe Customer ID "
                    "(must start with 'cus_')"
                ),
            }),
        )]

    # Auth gate — step 2: customer must have an active Stripe subscription.
    if not _verify_subscription(api_key_id):
        return [TextContent(
            type="text",
            text=json.dumps({
                "error": "Unauthorized",
                "message": (
                    "No active subscription found for this Customer ID. "
                    "Visit https://json-sanity.netlify.app to subscribe."
                ),
            }),
        )]

    response: list[TextContent]
    success = True

    if name == "validate_json":
        raw = arguments.get("json_string", "")
        try:
            parsed = validate_json(raw)
            response = [TextContent(type="text", text=json.dumps({"valid": True, "parsed": parsed}))]
        except ValueError as exc:
            success = False
            response = [TextContent(type="text", text=json.dumps({"valid": False, "error": str(exc)}))]

    elif name == "repair_json":
        raw = arguments.get("json_string", "")
        try:
            repaired, fixes = repair_json(raw)
            response = [TextContent(type="text", text=json.dumps({"repaired": repaired, "fixes_applied": fixes}))]
        except ValueError as exc:
            success = False
            response = [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    elif name == "sanitize_json_output":
        raw = arguments.get("raw_string", "")
        try:
            sanitized, fixes = sanitize_json_output(raw)
            log_sanitize_call(
                input_length=len(raw),
                repair_performed=bool(fixes),
                api_key_id=api_key_id,
            )
            response = [TextContent(type="text", text=json.dumps({"sanitized": sanitized, "fixes_applied": fixes}))]
        except ValueError as exc:
            success = False
            log_sanitize_call(input_length=len(raw), repair_performed=False, api_key_id=api_key_id)
            response = [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    elif name == "repair_string":
        raw_string = arguments.get("raw_string", "")
        schema = arguments.get("schema")
        result = repair_string(raw_string, schema=schema)
        success = result.get("ok", False)
        response = [TextContent(type="text", text=json.dumps(result))]

    else:
        success = False
        response = [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    if success:
        billing_service.record_invocation(api_key_id=api_key_id, tool_name=name)

    return response


# ── StreamableHTTP transport / Starlette app ──────────────────────────────────

session_manager = StreamableHTTPSessionManager(
    app=mcp,
    stateless=True,   # no in-process session map — safe behind any load balancer
    json_response=False,
)


@contextlib.asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    async with session_manager.run():
        yield


async def handle_health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "json-sanity"})


async def handle_root(request: Request) -> Response:
    return Response(status_code=404)


async def handle_mcp(request: Request) -> None:
    token = _request_api_key.set(request.query_params.get("api_key_id"))
    try:
        await session_manager.handle_request(request.scope, request.receive, request._send)
    finally:
        _request_api_key.reset(token)


async def handle_stripe_webhook(request: Request) -> JSONResponse:
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    if not webhook_secret:
        logger.warning("STRIPE_WEBHOOK_SECRET not set — webhook endpoint disabled")
        return JSONResponse({"error": "not configured"}, status_code=503)

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except stripe.error.SignatureVerificationError:
        logger.warning("Stripe webhook signature verification failed")
        return JSONResponse({"error": "invalid signature"}, status_code=400)
    except Exception as exc:
        logger.warning("Stripe webhook parse error: %s", exc)
        return JSONResponse({"error": "bad payload"}, status_code=400)

    # Parse payload as plain dict — Stripe SDK v15 returns typed objects
    # that don't support .get(), so we use the raw JSON for data access.
    event_dict = json.loads(payload)

    if event["type"] == "checkout.session.completed":
        session_obj = event_dict["data"]["object"]
        customer_id = session_obj.get("customer")
        subscription_id = session_obj.get("subscription")
        customer_email = (
            session_obj.get("customer_details", {}).get("email")
            or session_obj.get("customer_email")
        )

        # Add the graduated metered price to the subscription so overages bill.
        metered_price_id = os.environ.get("STRIPE_METERED_PRICE_ID", "")
        if subscription_id and metered_price_id:
            try:
                stripe.SubscriptionItem.create(
                    subscription=subscription_id,
                    price=metered_price_id,
                )
                logger.info("Added metered price to subscription %s", subscription_id)
            except Exception as exc:
                logger.warning("Failed to add metered price to subscription %s: %s", subscription_id, exc)

        if customer_id and customer_email:
            await _send_onboarding_email(customer_id, customer_email)
        else:
            logger.warning(
                "checkout.session.completed missing customer or email: %s",
                session_obj.get("id"),
            )

    return JSONResponse({"received": True})


async def _send_onboarding_email(customer_id: str, email: str) -> None:
    resend_key = os.environ.get("RESEND_API_KEY", "")
    from_email = os.environ.get("FROM_EMAIL", "onboarding@resend.dev")

    if not resend_key:
        logger.warning("RESEND_API_KEY not set — skipping onboarding email for %s", customer_id)
        return

    body = f"""Welcome to JSON-Sanity!

Your API key (Stripe Customer ID):

  {customer_id}

Pass it as api_key_id on every tool call. For Claude Desktop, add this
to your claude_desktop_config.json:

{{
  "mcpServers": {{
    "json-sanity": {{
      "url": "https://json-sanity.up.railway.app/mcp",
      "env": {{}}
    }}
  }}
}}

Then pass your key on each call:

  api_key_id: "{customer_id}"

Full docs and examples: https://json-sanity.netlify.app

Questions? Reply to this email or reach us at peakcalc@gmail.com
"""

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {resend_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": from_email,
                    "to": email,
                    "subject": "Your JSON-Sanity API key",
                    "text": body,
                },
                timeout=10.0,
            )
        if resp.status_code not in (200, 201):
            logger.warning("Resend returned %s for %s: %s", resp.status_code, email, resp.text[:200])
        else:
            logger.info("Onboarding email sent to %s (customer %s)", email, customer_id)
    except Exception as exc:
        logger.warning("Failed to send onboarding email to %s: %s", email, exc)


app = Starlette(
    lifespan=lifespan,
    routes=[
        Route("/", endpoint=handle_root),
        Route("/health", endpoint=handle_health),
        Route("/mcp", endpoint=handle_mcp, methods=["GET", "POST", "DELETE"]),
        Route("/stripe/webhook", endpoint=handle_stripe_webhook, methods=["POST"]),
    ],
)

# ── entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
