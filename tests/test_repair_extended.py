"""
Extended test suite covering gaps identified in the test coverage audit.

Covers:
  - validate_json (untested function)
  - repair_json (untested directly)
  - Python/JS literals (True, False, None, undefined)
  - Error/failure paths for all three entry points
  - Empty and whitespace-only input
  - sanitize_json_output Step 1 (literal \\n escape sequences)
  - sanitize_json_output Step 2 (illegal control chars / NUL bytes)
  - Markdown fence without language tag
  - repair_string with markdown fence
  - Top-level truncated array
  - Pure trailing comma (no other damage)
  - Deeply nested truncated structure
  - Response shape invariants (fix_actions always present, failure dict keys)
  - Schema validation: pass, missing field, wrong type, enum violation,
    additionalProperties, and invalid schema
  - Unicode in string values
"""

from __future__ import annotations

import json
import pytest

from repair_logic import (
    clean_llm_markdown,
    repair_json,
    repair_string,
    sanitize_json_output,
    validate_json,
)


# ── validate_json ─────────────────────────────────────────────────────────────

def test_validate_json_valid_object():
    parsed = validate_json('{"a": 1, "b": true}')
    assert parsed == {"a": 1, "b": True}


def test_validate_json_valid_array():
    parsed = validate_json('[1, "two", null]')
    assert parsed == [1, "two", None]


def test_validate_json_invalid_raises_with_location():
    with pytest.raises(ValueError, match="Invalid JSON"):
        validate_json('{"a":')


def test_validate_json_empty_raises():
    with pytest.raises(ValueError):
        validate_json("")


def test_validate_json_whitespace_only_raises():
    with pytest.raises(ValueError):
        validate_json("   \n  ")


# ── repair_json (direct) ──────────────────────────────────────────────────────

def test_repair_json_valid_passthrough():
    repaired, fixes = repair_json('{"x": 1}')
    assert json.loads(repaired) == {"x": 1}
    assert fixes == []


def test_repair_json_trailing_comma_only():
    repaired, fixes = repair_json('{"a": 1,}')
    assert json.loads(repaired) == {"a": 1}
    assert any("trailing comma" in f for f in fixes)


def test_repair_json_single_quotes():
    repaired, fixes = repair_json("{'key': 'val'}")
    assert json.loads(repaired) == {"key": "val"}
    assert any("single quote" in f for f in fixes)


def test_repair_json_unquoted_keys():
    repaired, fixes = repair_json('{key: "val", other: 2}')
    parsed = json.loads(repaired)
    assert parsed == {"key": "val", "other": 2}
    assert any("unquoted" in f for f in fixes)


def test_repair_json_python_true():
    repaired, fixes = repair_json('{"active": True}')
    assert json.loads(repaired) == {"active": True}
    assert any("True" in f for f in fixes)


def test_repair_json_python_false():
    repaired, fixes = repair_json('{"active": False}')
    assert json.loads(repaired) == {"active": False}
    assert any("False" in f for f in fixes)


def test_repair_json_python_none():
    repaired, fixes = repair_json('{"value": None}')
    assert json.loads(repaired) == {"value": None}
    assert any("None" in f for f in fixes)


def test_repair_json_js_undefined():
    repaired, fixes = repair_json('{"value": undefined}')
    assert json.loads(repaired) == {"value": None}
    assert any("undefined" in f for f in fixes)


def test_repair_json_multiple_literals():
    repaired, fixes = repair_json('{"a": True, "b": False, "c": None}')
    parsed = json.loads(repaired)
    assert parsed == {"a": True, "b": False, "c": None}


def test_repair_json_irrecoverable_raises():
    with pytest.raises(ValueError, match="Could not repair"):
        repair_json("this is not JSON at all {{{")


# ── repair_string: error / failure paths ──────────────────────────────────────

def test_repair_string_no_json_delimiters():
    result = repair_string("this has no braces or brackets")
    assert result["ok"] is False
    assert "No JSON" in result["error"]
    assert result["fixes_applied"] == []
    assert result["fix_actions"] == []


def test_repair_string_irrecoverable():
    result = repair_string("{{{totally broken")
    assert result["ok"] is False
    assert "error" in result


def test_repair_string_empty_string():
    result = repair_string("")
    assert result["ok"] is False


def test_repair_string_whitespace_only():
    result = repair_string("   \n  ")
    assert result["ok"] is False


# ── sanitize_json_output: error paths ────────────────────────────────────────

def test_sanitize_empty_string_raises():
    with pytest.raises(ValueError):
        sanitize_json_output("")


def test_sanitize_no_json_raises():
    with pytest.raises(ValueError):
        sanitize_json_output("this is just plain text")


# ── sanitize_json_output: Step 1 — literal \\n sequences ─────────────────────

def test_sanitize_literal_backslash_n_outside_strings():
    # LLM writes {\n  "key": "val"\n} with literal backslash-n between tokens
    # instead of real newlines — step 1 decodes them so JSON parses cleanly.
    raw = '{\\n  "key": "val"\\n}'  # actual chars: { \ n   " k e y " ...
    sanitized, fixes = sanitize_json_output(raw)
    assert json.loads(sanitized) == {"key": "val"}
    assert any("decoded" in f for f in fixes)


def test_sanitize_literal_backslash_t_outside_strings():
    raw = '{\\t"key":\\t"val"}'
    sanitized, fixes = sanitize_json_output(raw)
    assert json.loads(sanitized) == {"key": "val"}
    assert any("decoded" in f for f in fixes)


# ── sanitize_json_output: Step 2 — illegal control characters ────────────────

def test_sanitize_nul_byte_stripped():
    # NUL byte (x00) embedded in raw output between tokens
    raw = '{"key": \x00"val"}'
    sanitized, fixes = sanitize_json_output(raw)
    assert json.loads(sanitized) == {"key": "val"}
    assert any("control" in f or "illegal" in f for f in fixes)


def test_sanitize_bell_character_stripped():
    # BEL (x07) — another illegal control char outside x09/x0a/x0d range
    raw = '{"key": \x07"val"}'
    sanitized, fixes = sanitize_json_output(raw)
    assert json.loads(sanitized) == {"key": "val"}
    assert any("control" in f or "illegal" in f for f in fixes)


# ── markdown fence variants ───────────────────────────────────────────────────

def test_markdown_fence_no_language_tag():
    raw = "```\n{\"key\": \"value\"}\n```"
    sanitized, fixes = sanitize_json_output(raw)
    assert json.loads(sanitized) == {"key": "value"}
    assert any("markdown" in f or "fence" in f for f in fixes)


def test_markdown_fence_uppercase_json_tag():
    raw = "```JSON\n{\"key\": \"value\"}\n```"
    sanitized, fixes = sanitize_json_output(raw)
    assert json.loads(sanitized) == {"key": "value"}
    assert any("markdown" in f or "fence" in f for f in fixes)


def test_repair_string_markdown_fence():
    raw = "```json\n{\"key\": \"value\"}\n```"
    result = repair_string(raw)
    assert result["ok"] is True
    assert result["parsed"] == {"key": "value"}
    assert any("fence" in f or "markdown" in f for f in result["fixes_applied"])


def test_repair_string_markdown_fence_no_tag():
    raw = "```\n{\"x\": 1}\n```"
    result = repair_string(raw)
    assert result["ok"] is True
    assert result["parsed"] == {"x": 1}
    assert any("fence" in f or "markdown" in f for f in result["fixes_applied"])


# ── top-level truncated array ─────────────────────────────────────────────────

def test_repair_string_truncated_top_level_array():
    raw = '[1, 2, 3'
    result = repair_string(raw)
    assert result["ok"] is True
    assert result["parsed"] == [1, 2, 3]
    assert any("bracket" in f or "closing" in f for f in result["fixes_applied"])


def test_sanitize_truncated_top_level_array():
    raw = '[{"a": 1}, {"b": 2'
    sanitized, fixes = sanitize_json_output(raw)
    parsed = json.loads(sanitized)
    assert parsed[0] == {"a": 1}


# ── pure trailing comma ───────────────────────────────────────────────────────

def test_repair_string_pure_trailing_comma():
    raw = '{"key": "val",}'
    result = repair_string(raw)
    assert result["ok"] is True
    assert result["parsed"] == {"key": "val"}
    assert any("trailing comma" in f for f in result["fixes_applied"])


def test_repair_string_trailing_comma_in_array():
    raw = '[1, 2, 3,]'
    result = repair_string(raw)
    assert result["ok"] is True
    assert result["parsed"] == [1, 2, 3]
    assert any("trailing comma" in f for f in result["fixes_applied"])


# ── deeply nested truncated structure ────────────────────────────────────────

def test_repair_string_deep_nesting_truncated():
    raw = '{"a": {"b": [1, 2'
    result = repair_string(raw)
    assert result["ok"] is True
    assert result["parsed"]["a"]["b"] == [1, 2]


def test_repair_string_triple_nesting_truncated():
    raw = '{"x": {"y": {"z": "val"'
    result = repair_string(raw)
    assert result["ok"] is True
    assert result["parsed"]["x"]["y"]["z"] == "val"


# ── response shape invariants ─────────────────────────────────────────────────

def test_repair_string_fix_actions_always_present_on_success():
    result = repair_string('{"x": 1}')
    assert "fix_actions" in result


def test_repair_string_fix_actions_always_present_on_failure():
    result = repair_string("no json here")
    assert "fix_actions" in result
    assert isinstance(result["fix_actions"], list)


def test_repair_string_failure_dict_has_required_keys():
    result = repair_string("no json here")
    assert result["ok"] is False
    assert "error" in result
    assert "fixes_applied" in result
    assert "fix_actions" in result


def test_repair_string_success_dict_has_required_keys():
    result = repair_string('{"x": 1}')
    assert "ok" in result
    assert "repaired" in result
    assert "parsed" in result
    assert "fixes_applied" in result
    assert "fix_actions" in result


# ── Unicode / international characters ───────────────────────────────────────

def test_repair_string_unicode_preserved():
    raw = '{"name": "用户", "greeting": "こんにちは"}'
    result = repair_string(raw)
    assert result["ok"] is True
    assert result["parsed"] == {"name": "用户", "greeting": "こんにちは"}
    assert result["fixes_applied"] == []


def test_repair_string_emoji_preserved():
    raw = '{"icon": "🔥", "status": "✅"}'
    result = repair_string(raw)
    assert result["ok"] is True
    assert result["parsed"]["icon"] == "🔥"
    assert result["fixes_applied"] == []


# ── schema validation ─────────────────────────────────────────────────────────

_SCHEMA = {
    "type": "object",
    "required": ["user_id", "action"],
    "properties": {
        "user_id": {"type": "string"},
        "action": {"type": "string", "enum": ["create", "update", "delete"]},
        "count": {"type": "integer"},
    },
    "additionalProperties": False,
}


def test_schema_valid_passes():
    raw = '{"user_id": "abc", "action": "create"}'
    result = repair_string(raw, schema=_SCHEMA)
    assert result["ok"] is True
    assert result["fix_actions"] == []


def test_schema_missing_required_field():
    raw = '{"user_id": "abc"}'  # missing "action"
    result = repair_string(raw, schema=_SCHEMA)
    assert result["ok"] is False
    assert any("action" in a for a in result["fix_actions"])
    assert any("Add" in a or "required" in a.lower() for a in result["fix_actions"])


def test_schema_wrong_type():
    raw = '{"user_id": 123, "action": "create"}'  # user_id must be string
    result = repair_string(raw, schema=_SCHEMA)
    assert result["ok"] is False
    assert any("user_id" in a for a in result["fix_actions"])
    assert any("string" in a for a in result["fix_actions"])


def test_schema_enum_violation():
    raw = '{"user_id": "abc", "action": "destroy"}'  # "destroy" not in enum
    result = repair_string(raw, schema=_SCHEMA)
    assert result["ok"] is False
    assert any("action" in a for a in result["fix_actions"])


def test_schema_additional_properties():
    raw = '{"user_id": "abc", "action": "create", "extra_field": "bad"}'
    result = repair_string(raw, schema=_SCHEMA)
    assert result["ok"] is False
    assert any("extra_field" in a or "unexpected" in a.lower() for a in result["fix_actions"])


def test_schema_multiple_violations():
    # Missing action, wrong type for user_id, extra field
    raw = '{"user_id": 999, "extra": "oops"}'
    result = repair_string(raw, schema=_SCHEMA)
    assert result["ok"] is False
    assert len(result["fix_actions"]) >= 2


def test_schema_invalid_schema_itself():
    bad_schema = {"type": "not_a_valid_type"}
    raw = '{"x": 1}'
    result = repair_string(raw, schema=bad_schema)
    assert result["ok"] is False
    assert any("invalid" in a.lower() or "schema" in a.lower() for a in result["fix_actions"])


def test_schema_count_wrong_type():
    raw = '{"user_id": "abc", "action": "create", "count": "five"}'
    result = repair_string(raw, schema=_SCHEMA)
    assert result["ok"] is False
    assert any("count" in a for a in result["fix_actions"])
    assert any("integer" in a for a in result["fix_actions"])


def test_schema_repair_then_validate():
    # Input needs structural repair AND has a schema violation
    raw = '{"user_id": "abc", "action": "create", "count": 5,}'  # trailing comma
    result = repair_string(raw, schema=_SCHEMA)
    assert result["ok"] is True  # trailing comma fixed, schema passes
    assert any("trailing comma" in f for f in result["fixes_applied"])
    assert result["fix_actions"] == []
