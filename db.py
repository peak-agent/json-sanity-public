"""
Supabase client and logging helpers.
"""

from __future__ import annotations

import logging
import os

from supabase import Client, create_client

logger = logging.getLogger(__name__)

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        _client = create_client(url, key)
    return _client


def log_sanitize_call(
    *,
    input_length: int,
    repair_performed: bool,
    api_key_id: str | None,
) -> None:
    """
    Insert one row into the sanitize_logs table.
    Failures are logged as warnings so they never break the tool response.
    """
    try:
        get_client().table("sanitize_logs").insert(
            {
                "input_length": input_length,
                "repair_performed": repair_performed,
                "api_key_id": api_key_id,
            }
        ).execute()
    except Exception as exc:
        logger.warning("Failed to write sanitize log: %s", exc)
