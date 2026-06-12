import json
import pytest

from repair_logic import repair_string, sanitize_json_output


# ── prose preamble ────────────────────────────────────────────────────────────

def test_prose_preamble_stripped():
    raw = 'Sure, here is your data: {"name": "Alice", "age": 30}'
    result = repair_string(raw)
    assert result["ok"] is True
    assert result["parsed"] == {"name": "Alice", "age": 30}
    assert any("preamble" in f for f in result["fixes_applied"])


def test_prose_preamble_and_suffix_stripped():
    raw = 'Here you go: {"key": "val"} Hope that helps!'
    result = repair_string(raw)
    assert result["ok"] is True
    assert result["parsed"] == {"key": "val"}
    assert any("suffix" in f for f in result["fixes_applied"])


# ── unescaped newlines inside string values ───────────────────────────────────

def test_unescaped_newline_in_string():
    # A real newline character embedded inside a JSON string value
    raw = '{"message": "line one\nline two"}'
    result = repair_string(raw)
    assert result["ok"] is True
    parsed = result["parsed"]
    assert "line one" in parsed["message"]
    assert "line two" in parsed["message"]
    assert any("control character" in f for f in result["fixes_applied"])


def test_unescaped_tab_and_carriage_return_in_string():
    raw = '{"data": "col1\tcol2\r\ncol3"}'
    result = repair_string(raw)
    assert result["ok"] is True
    assert any("control character" in f for f in result["fixes_applied"])


# ── missing closing brace ─────────────────────────────────────────────────────

def test_missing_closing_brace():
    raw = '{"name": "Bob", "active": true'
    result = repair_string(raw)
    assert result["ok"] is True
    assert result["parsed"]["name"] == "Bob"
    assert result["parsed"]["active"] is True
    assert any("closing bracket" in f for f in result["fixes_applied"])


def test_missing_closing_bracket_in_nested_array():
    raw = '{"items": [1, 2, 3}'
    result = repair_string(raw)
    assert result["ok"] is True
    assert result["parsed"]["items"] == [1, 2, 3]


# ── valid JSON returned untouched ─────────────────────────────────────────────

def test_valid_json_untouched():
    raw = '{"status": "ok", "count": 42}'
    result = repair_string(raw)
    assert result["ok"] is True
    assert result["fixes_applied"] == []
    assert json.loads(result["repaired"]) == {"status": "ok", "count": 42}


def test_valid_json_array_untouched():
    raw = '[1, 2, 3]'
    result = repair_string(raw)
    assert result["ok"] is True
    assert result["fixes_applied"] == []
    assert result["parsed"] == [1, 2, 3]


# ── sanitize_json_output ──────────────────────────────────────────────────────

def test_sanitize_strips_preamble():
    raw = "Here is the JSON: {\"x\": 1}"
    sanitized, fixes = sanitize_json_output(raw)
    assert json.loads(sanitized) == {"x": 1}
    assert any("preamble" in f for f in fixes)


def test_sanitize_valid_passthrough():
    raw = '{"clean": true}'
    sanitized, fixes = sanitize_json_output(raw)
    assert json.loads(sanitized) == {"clean": True}
    assert fixes == []


def test_sanitize_escapes_embedded_newline_in_string():
    raw = '{"msg": "line one\nline two"}'
    sanitized, fixes = sanitize_json_output(raw)
    parsed = json.loads(sanitized)
    assert "line one" in parsed["msg"] and "line two" in parsed["msg"]
    assert any("control character" in f for f in fixes)


def test_sanitize_markdown_fence_stripped():
    raw = "```json\n{\"key\": \"value\"}\n```"
    sanitized, fixes = sanitize_json_output(raw)
    assert json.loads(sanitized) == {"key": "value"}
    assert any("markdown" in f or "fence" in f for f in fixes)
