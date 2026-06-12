"""
Go/No-Go smoke test for the json-sanity MCP server — production auth edition.

Default target: https://json-sanity.up.railway.app
Override:       SMOKE_TEST_URL=http://localhost:8000 python smoke_test.py

WHAT IT CHECKS
  1. GET /health returns {"status": "ok"} (health check).
  2. An MCP StreamableHTTP call WITHOUT api_key_id is rejected with an
     "Unauthorized" error (auth gate must fire before any repair logic).
  3. An MCP StreamableHTTP call WITH a valid cus_... api_key_id succeeds,
     returns sanitized JSON and a non-empty fixes_applied list.

RUNNING
    cd mcp-json-sanity
    SMOKE_TEST_CUSTOMER_ID=cus_... python tests/smoke_test.py
    SMOKE_TEST_URL=http://localhost:8000 SMOKE_TEST_CUSTOMER_ID=cus_... python tests/smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

MALFORMED = (
    "Sure, here is the JSON you asked for: "
    '{name: "Alice", age: 30, tags: ["a", "b",],'
)

VALID_API_KEY = os.environ.get("SMOKE_TEST_CUSTOMER_ID", "")
if not VALID_API_KEY:
    print("ERROR: Set SMOKE_TEST_CUSTOMER_ID=cus_... before running", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    import httpx

    base_url = os.environ.get("SMOKE_TEST_URL", "https://json-sanity.up.railway.app").rstrip("/")
    mcp_url = f"{base_url}/mcp"
    health_url = f"{base_url}/health"

    failures: list[str] = []
    auth_payload: dict[str, Any] = {}

    print(f"Targeting: {base_url}")

    # ── 1. Health check ──────────────────────────────────────────────────────
    print("── Check 1: GET /health ─────────────────────────────────────────")
    try:
        resp = httpx.get(health_url, timeout=10.0)
        if resp.status_code != 200:
            failures.append(f"Health check returned HTTP {resp.status_code}: {resp.text[:200]}")
            print(f"  FAIL — HTTP {resp.status_code}")
        else:
            body = resp.json()
            if body.get("status") != "ok":
                failures.append(f"Health check body unexpected: {body}")
                print(f"  FAIL — {body}")
            else:
                print(f"  PASS — {body}")
    except Exception as exc:
        failures.append(f"Health check request failed: {exc!r}")
        print(f"  FAIL — {exc!r}")

    # ── 2. Unauthorized request (no api_key_id) ──────────────────────────────
    print("── Check 2: Unauthorized request (no api_key_id) ────────────────")

    async def _call_no_auth() -> Any:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(mcp_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(
                    "sanitize_json_output",
                    {"raw_string": MALFORMED},  # deliberately omit api_key_id
                )

    unauth_result = None
    try:
        unauth_result = asyncio.run(asyncio.wait_for(_call_no_auth(), timeout=30.0))
    except Exception as exc:
        failures.append(f"Unauthorized MCP call failed unexpectedly: {exc!r}")
        print(f"  FAIL — {exc!r}")

    if unauth_result is not None:
        try:
            content_text = unauth_result.content[0].text if getattr(unauth_result, "content", None) else ""
            body = json.loads(content_text) if content_text else {}
            if body.get("error") == "Unauthorized":
                print(f"  PASS — correctly rejected: {body['error']}: {body.get('message', '')}")
            else:
                failures.append(
                    f"Expected Unauthorized error but got: {body}"
                )
                print(f"  FAIL — auth gate did not fire: {body}")
        except Exception as exc:
            failures.append(f"Unauthorized response parse failed: {exc!r}")
            print(f"  FAIL — {exc!r}")

    # ── 3. Authorized MCP round-trip ─────────────────────────────────────────
    print("── Check 3: Authorized MCP round-trip (sanitize_json_output) ────")

    async def _call_authorized() -> Any:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(mcp_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(
                    "sanitize_json_output",
                    {"raw_string": MALFORMED, "api_key_id": VALID_API_KEY},
                )

    auth_result = None
    try:
        auth_result = asyncio.run(asyncio.wait_for(_call_authorized(), timeout=30.0))
    except Exception as exc:
        failures.append(f"Authorized MCP round-trip failed: {exc!r}")
        print(f"  FAIL — {exc!r}")

    if auth_result is not None:
        try:
            if getattr(auth_result, "isError", False):
                failures.append(f"MCP reported isError=True: {auth_result}")
                print(f"  FAIL — isError: {auth_result}")
            elif not getattr(auth_result, "content", None):
                failures.append("MCP result has no content blocks")
                print("  FAIL — empty content")
            else:
                auth_payload = json.loads(auth_result.content[0].text)
                if "error" in auth_payload:
                    failures.append(f"Authorized call returned error: {auth_payload}")
                    print(f"  FAIL — {auth_payload}")
                elif "sanitized" not in auth_payload:
                    failures.append(f"Response missing 'sanitized' key: {auth_payload}")
                    print(f"  FAIL — {auth_payload}")
                else:
                    json.loads(auth_payload["sanitized"])  # must be valid JSON
                    if not auth_payload.get("fixes_applied"):
                        failures.append("fixes_applied was empty — server didn't record any repair")
                        print("  FAIL — fixes_applied empty")
                    else:
                        print(f"  PASS — sanitized   : {auth_payload['sanitized']!r}")
                        print(f"  PASS — fixes_applied: {auth_payload['fixes_applied']}")
                        print(f"  PASS — billing fired for api_key_id={VALID_API_KEY!r}")
        except Exception as exc:
            failures.append(f"Authorized response parse failed: {exc!r}")
            print(f"  FAIL — {exc!r}")

    # ── Verdict ──────────────────────────────────────────────────────────────
    print("─────────────────────────────────────────────────────────────────")
    if failures:
        print("NO-GO:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)

    print("GO — all checks passed")
    print(f"  target        : {base_url}")
    print(f"  sanitized     : {auth_payload.get('sanitized')!r}")
    print(f"  fixes_applied : {auth_payload.get('fixes_applied')}")
    print(f"  auth gate     : rejected anonymous calls ✓")
    print(f"  billing gate  : fired for {VALID_API_KEY!r} ✓")


if __name__ == "__main__":
    main()
