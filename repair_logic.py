"""
Core JSON repair heuristics — no heavy deps, safe to bundle on a Worker.
"""

from __future__ import annotations

import json
import re

# `jsonschema` is imported lazily inside _validate_against_schema so that
# environments that don't need schema enforcement don't pay the import cost.


def clean_llm_markdown(text: str) -> tuple[str, bool]:
    """
    Strip markdown code fences that LLMs wrap around JSON output.

    Handles: ```json\\n...\\n```, ```\\n...\\n```, and the no-newline
    variant.  Returns (cleaned_text, was_changed).
    """
    stripped = text.strip()
    cleaned = re.sub(r'^```(?:json|JSON)?\s*\n?', '', stripped)
    cleaned = re.sub(r'\n?```\s*$', '', cleaned)
    changed = cleaned != stripped
    return cleaned, changed


def repair_json(raw: str) -> tuple[str, list[str]]:
    """
    Attempt to repair malformed JSON.

    Returns (repaired_json_str, list_of_applied_fixes).
    Raises ValueError if the input cannot be salvaged.
    """
    fixes: list[str] = []
    text = raw.strip()

    # 1. Already valid — nothing to do
    if _try_parse(text) is not None:
        return text, fixes

    # 2. Strip trailing commas before ] or }
    cleaned = re.sub(r",\s*([}\]])", r"\1", text)
    if cleaned != text:
        fixes.append("removed trailing commas")
        text = cleaned

    # 3. Replace single quotes used as string delimiters
    single_quoted = re.sub(r"'([^']*)'", r'"\1"', text)
    if single_quoted != text:
        fixes.append("replaced single quotes with double quotes")
        text = single_quoted

    # 4. Unquoted keys  { key: "val" } → { "key": "val" }
    unquoted_key = re.sub(r'([{,]\s*)([A-Za-z_]\w*)(\s*:)', r'\1"\2"\3', text)
    if unquoted_key != text:
        fixes.append("quoted unquoted object keys")
        text = unquoted_key

    # 5. Python/JS literals → JSON literals
    literal_map = {"True": "true", "False": "false", "None": "null", "undefined": "null"}
    for src, dst in literal_map.items():
        pattern = rf'\b{src}\b'
        replaced = re.sub(pattern, dst, text)
        if replaced != text:
            fixes.append(f"replaced {src} → {dst}")
            text = replaced

    # 6. Truncated JSON — try to close open brackets/braces
    result = _try_parse(text)
    if result is None:
        text, extra_fixes = _close_open_brackets(text)
        fixes.extend(extra_fixes)

    result = _try_parse(text)
    if result is None:
        raise ValueError("Could not repair JSON")

    return text, fixes


def validate_json(raw: str) -> dict:
    """
    Return parsed object if valid, raise ValueError with a descriptive message otherwise.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON at line {exc.lineno}, col {exc.colno}: {exc.msg}") from exc


def sanitize_json_output(raw_string: str) -> tuple[str, list[str]]:
    """
    Strip prose preambles and repair malformed control characters, then
    return valid JSON. Raises ValueError if the result cannot be parsed.

    Returns (sanitized_json_str, list_of_applied_fixes).
    """
    fixes: list[str] = []

    # 0. Strip markdown code fences (```json ... ```)
    text, fenced = clean_llm_markdown(raw_string)
    if fenced:
        fixes.append("stripped markdown code fence")

    # 1. Replace literal \n \t \r escape sequences that appear outside strings
    ctrl_cleaned = re.sub(r'\\([nrt])', lambda m: {"n": "\n", "r": "\r", "t": "\t"}[m.group(1)], text)
    if ctrl_cleaned != text:
        fixes.append("decoded escaped control characters (\\n/\\r/\\t)")
        text = ctrl_cleaned

    # 2. Remove actual unescaped control characters (0x00–0x1F except \n \r \t)
    no_ctrl = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
    if no_ctrl != text:
        fixes.append("removed illegal control characters")
        text = no_ctrl

    # 3. Strip prose preamble — everything before the first { or [
    match = re.search(r'[{\[]', text)
    if match and match.start() > 0:
        fixes.append(f"stripped {match.start()}-char prose preamble")
        text = text[match.start():]

    # 4. Strip prose suffix — everything after the last } or ]
    last = max(text.rfind("}"), text.rfind("]"))
    if last != -1 and last < len(text) - 1:
        fixes.append(f"stripped {len(text) - last - 1}-char prose suffix")
        text = text[: last + 1]

    # 5. Escape raw control characters inside string values (mirrors repair_string step 2)
    escaped, escape_count = _escape_control_chars_in_strings(text)
    if escape_count:
        fixes.append(
            f"escaped {escape_count} unescaped control character(s) inside string values"
        )
        text = escaped

    # 6. Delegate remaining structural issues to repair_json
    if _try_parse(text) is None:
        text, repair_fixes = repair_json(text)
        fixes.extend(repair_fixes)

    if _try_parse(text) is None:
        raise ValueError("Could not sanitize input into valid JSON")

    return text, fixes


# ── Iteration 2: Deterministic Repair Engine ─────────────────────────────────

def repair_string(raw: str, schema: dict | None = None) -> dict:
    """
    Deterministic repair pipeline for arbitrary strings that should contain JSON.

    Steps (in order):
      1. Regex-locate the first `{`/`[` and last `}`/`]` — slice away prose
         preambles and suffixes.
      2. Escape unescaped control characters (real newlines, tabs, carriage
         returns) found *inside* string values so the result is legal JSON.
      3. Validate with ``json.loads``. If it still fails, run the structural
         repair pipeline (trailing commas, unquoted keys, etc.) and then a
         partial-recovery pass that closes any unclosed brackets.
      4. (Optional) If a JSON schema is supplied, validate the parsed object
         with the ``jsonschema`` library. On failure, translate each
         validation error into a concrete "Fix Action" string the calling
         agent can act on.

    Returns a dict:
      {
        "ok":            bool,   # final repaired JSON passes structural + schema checks
        "repaired":      str,    # repaired JSON string (present whenever parsable)
        "parsed":        object, # the parsed object (present whenever parsable)
        "fixes_applied": list,   # human-readable audit trail of transforms applied
        "fix_actions":   list,   # remaining actions for the agent (from schema errors)
        "error":         str,    # only set when the string could not be salvaged at all
      }
    """
    fixes: list[str] = []

    # ── Step 0. Strip markdown code fences (```json ... ```) ─────────────
    raw, fenced = clean_llm_markdown(raw)
    if fenced:
        fixes.append("stripped markdown code fence")

    # ── Step 1. Strip prose preambles/suffixes via regex ─────────────────
    first = re.search(r'[{\[]', raw)
    if not first:
        return {
            "ok": False,
            "error": "No JSON object or array delimiter found in input",
            "fixes_applied": fixes,
            "fix_actions": [],
        }

    last_brace = raw.rfind('}')
    last_bracket = raw.rfind(']')
    last = max(last_brace, last_bracket)

    if last < first.start():
        # Opener found but no closer — keep everything from the opener
        # onward and let partial recovery close the structure later.
        text = raw[first.start():]
        if first.start() > 0:
            fixes.append(
                f"stripped {first.start()}-char prose preamble "
                "(no closing bracket found — partial recovery will close)"
            )
    else:
        text = raw[first.start(): last + 1]
        if first.start() > 0:
            fixes.append(f"stripped {first.start()}-char prose preamble")
        trailing = len(raw) - last - 1
        if trailing > 0:
            fixes.append(f"stripped {trailing}-char prose suffix")

    # ── Step 2. Escape unescaped control chars inside string values ──────
    escaped, escape_count = _escape_control_chars_in_strings(text)
    if escape_count:
        fixes.append(
            f"escaped {escape_count} unescaped control character(s) inside string values"
        )
        text = escaped

    # ── Step 3. Validate; fall back to structural repair, then partial
    #            recovery (close unclosed brackets) if json.loads still fails.
    parsed = _try_parse(text)

    if parsed is None:
        try:
            text, structural_fixes = repair_json(text)
            fixes.extend(structural_fixes)
            parsed = _try_parse(text)
        except ValueError:
            # repair_json exhausted its heuristics — try partial recovery below.
            pass

    if parsed is None:
        text, bracket_fixes = _close_open_brackets(text)
        fixes.extend(bracket_fixes)
        parsed = _try_parse(text)

    if parsed is None:
        return {
            "ok": False,
            "error": "Could not repair input into valid JSON",
            "repaired": text,
            "fixes_applied": fixes,
            "fix_actions": [],
        }

    # ── Step 4. Optional schema enforcement ──────────────────────────────
    fix_actions: list[str] = []
    if schema is not None:
        fix_actions = _validate_against_schema(parsed, schema)

    return {
        "ok": not fix_actions,
        "repaired": text,
        "parsed": parsed,
        "fixes_applied": fixes,
        "fix_actions": fix_actions,
    }


# ── helpers ──────────────────────────────────────────────────────────────────

def _try_parse(text: str) -> object | None:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _escape_control_chars_in_strings(text: str) -> tuple[str, int]:
    """
    Walk `text` as a JSON-ish stream and escape raw \\n / \\r / \\t characters
    that appear *inside* string values. Characters outside strings (between
    tokens) are left alone — JSON permits whitespace there.

    Returns (rewritten_text, num_characters_escaped).
    """
    out: list[str] = []
    in_string = False
    escape_next = False
    escape_map = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
    count = 0

    for ch in text:
        if escape_next:
            # Previous char was an in-string backslash — pass this one through
            # verbatim so we don't disturb legitimate JSON escape sequences.
            out.append(ch)
            escape_next = False
            continue
        if in_string and ch == "\\":
            out.append(ch)
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if in_string and ch in escape_map:
            out.append(escape_map[ch])
            count += 1
            continue
        out.append(ch)

    return "".join(out), count


def _close_open_brackets(text: str) -> tuple[str, list[str]]:
    fixes: list[str] = []
    stack: list[str] = []  # expected closers
    in_string = False
    escape_next = False
    result: list[str] = []

    for ch in text:
        if escape_next:
            escape_next = False
            result.append(ch)
            continue
        if ch == "\\" and in_string:
            escape_next = True
            result.append(ch)
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            continue
        if in_string:
            result.append(ch)
            continue
        if ch in "{[":
            stack.append("}" if ch == "{" else "]")
            result.append(ch)
        elif ch in "}]":
            if stack and stack[-1] != ch:
                # Mismatched closer — insert the expected one, then keep ch
                expected = stack.pop()
                fixes.append(f"replaced mismatched '{ch}' with '{expected}{ch}'")
                result.append(expected)
                result.append(ch)
                # ch may now match the new stack top
                if stack and stack[-1] == ch:
                    stack.pop()
            elif stack:
                stack.pop()
                result.append(ch)
            else:
                result.append(ch)
        else:
            result.append(ch)

    if stack:
        closing = "".join(reversed(stack))
        fixes.append(f"appended closing brackets: {closing!r}")
        result.append(closing)

    return "".join(result), fixes


def _validate_against_schema(instance: object, schema: dict) -> list[str]:
    """
    Validate `instance` against `schema` using the ``jsonschema`` library and
    return a list of concrete "Fix Action" strings describing exactly what the
    agent must change. Returns an empty list when the instance is valid.
    """
    try:
        import jsonschema  # type: ignore
    except ImportError:
        return [
            "Install the 'jsonschema' Python package to enable schema validation "
            "(pip install jsonschema)"
        ]

    # Pick the newest validator class this installation supports. jsonschema
    # renamed its draft validators over time; we fall back gracefully so the
    # tool works on older environments too.
    Validator = None
    for candidate in (
        "Draft202012Validator",
        "Draft201909Validator",
        "Draft7Validator",
        "Draft6Validator",
        "Draft4Validator",
    ):
        Validator = getattr(jsonschema, candidate, None)
        if Validator is not None:
            break
    if Validator is None:
        return ["The installed 'jsonschema' version is too old — upgrade to >=3.0"]

    try:
        Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:  # type: ignore[attr-defined]
        return [f"The supplied schema is itself invalid: {exc.message}"]

    validator = Validator(schema)
    actions: list[str] = []

    for err in sorted(
        validator.iter_errors(instance),
        key=lambda e: (list(e.absolute_path), e.validator or ""),
    ):
        path = ".".join(str(p) for p in err.absolute_path) or "$"
        kind = err.validator

        if kind == "required":
            # err.message looks like: "'name' is a required property"
            missing = err.message.split("'")[1] if "'" in err.message else "?"
            parent = path if path != "$" else "$"
            actions.append(f"Add required field '{missing}' at {parent}")
        elif kind == "type":
            expected = err.validator_value
            got = type(err.instance).__name__
            expected_str = (
                expected if isinstance(expected, str) else " or ".join(expected)
            )
            actions.append(
                f"Change '{path}' from type {got} to type {expected_str}"
            )
        elif kind == "additionalProperties":
            actions.append(
                f"Remove unexpected properties at '{path}' ({err.message})"
            )
        elif kind == "enum":
            actions.append(
                f"Change '{path}' to one of: {err.validator_value}"
            )
        elif kind == "const":
            actions.append(
                f"Set '{path}' to exactly {err.validator_value!r}"
            )
        elif kind == "pattern":
            actions.append(
                f"Make '{path}' match regex pattern: {err.validator_value}"
            )
        elif kind == "format":
            actions.append(f"Make '{path}' a valid {err.validator_value}")
        elif kind == "minLength":
            actions.append(
                f"Ensure '{path}' has at least {err.validator_value} character(s)"
            )
        elif kind == "maxLength":
            actions.append(
                f"Ensure '{path}' has at most {err.validator_value} character(s)"
            )
        elif kind == "minimum":
            actions.append(f"Ensure '{path}' >= {err.validator_value}")
        elif kind == "maximum":
            actions.append(f"Ensure '{path}' <= {err.validator_value}")
        elif kind == "exclusiveMinimum":
            actions.append(f"Ensure '{path}' > {err.validator_value}")
        elif kind == "exclusiveMaximum":
            actions.append(f"Ensure '{path}' < {err.validator_value}")
        elif kind == "minItems":
            actions.append(
                f"Ensure '{path}' has at least {err.validator_value} item(s)"
            )
        elif kind == "maxItems":
            actions.append(
                f"Ensure '{path}' has at most {err.validator_value} item(s)"
            )
        elif kind == "uniqueItems":
            actions.append(f"Ensure all items in '{path}' are unique")
        else:
            actions.append(f"Fix '{path}' ({kind}): {err.message}")

    return actions
