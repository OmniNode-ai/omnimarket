# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Semantic-validation false-RED regression tests (OMN-13217).

The OMN-13166 fix added a behavioral sandbox that catches false-greens, but the
2026-06-18 dev SEA/complexity matrix surfaced the inverse failure mode: the
sandbox false-REJECTs behaviorally-CORRECT handlers. Two distinct defects, both
captured here so neither can regress (and OMN-13166's false-green protection is
re-asserted alongside):

1. The restricted-builtins sandbox raised ``ImportError: __import__ not found``
   on any module-level ``import`` — even a safe stdlib import such as ``import
   re`` — so a correct camelCase handler that imported ``re`` (used OR unused)
   was rejected. Evidence: dev cell ``M1-MEDIUM-CAMEL-EXEMPLAR``
   (``import re`` unused) and ``C1-COMPLEX-JSONNORM-EXEMPLAR`` (``re`` used).
2. ``derive_semantic_fixtures`` matched the bare substring ``uppercase`` inside
   the structured task "normalizes ticket_id to uppercase OMN-123 form" and
   derived plain whole-string uppercase fixtures (``hello`` -> ``HELLO``) that
   are irrelevant to the ticket's actual transform, judging the handler against
   the wrong invariant. Evidence: dev cell
   ``C1-COMPLEX-JSONNORM-EXEMPLAR/fixture-derivation-defect.txt``.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_generation_consumer.semantic_validation import (
    derive_semantic_fixtures,
    evaluate_handler_semantics,
)

# ---------------------------------------------------------------------------
# Defect 1: stdlib imports must be permitted inside the semantic sandbox.
# ---------------------------------------------------------------------------

_CAMEL_TASK = (
    "Generate an ONEX compute node named postfix_camel_case_m1_dev that accepts "
    "snake_case text, trims it, converts it to camelCase, and returns "
    "camel_case plus changed boolean."
)

# M1-MEDIUM-CAMEL-EXEMPLAR: behaviorally correct, but imports `re` and never
# uses it. The pre-fix sandbox rejected this for the unused import alone.
_CORRECT_CAMEL_UNUSED_IMPORT = """\
import re

def handle(input_data):
    text = input_data.get("text", "")
    trimmed = text.strip()
    parts = trimmed.split("_")
    camel_case = parts[0].lower() + "".join(p.capitalize() for p in parts[1:])
    changed = camel_case != text
    return {"camel_case": camel_case, "changed": changed}
"""

# C1 family: behaviorally correct and genuinely uses `re`.
_CORRECT_CAMEL_USED_IMPORT = """\
import re

def handle(input_data):
    text = input_data.get("text", "")
    trimmed = text.strip()
    parts = [p for p in re.split(r"_+", trimmed) if p]
    camel_case = (parts[0].lower() + "".join(p.capitalize() for p in parts[1:])) if parts else ""
    changed = camel_case != text
    return {"camel_case": camel_case, "changed": changed}
"""


@pytest.mark.unit
def test_correct_handler_with_unused_re_import_passes() -> None:
    """A correct camelCase handler that imports (but never uses) `re` must pass."""
    fixtures = derive_semantic_fixtures(_CAMEL_TASK)
    result = evaluate_handler_semantics(_CORRECT_CAMEL_UNUSED_IMPORT, fixtures)
    assert result.checked is True
    assert result.passed is True, result.errors
    assert result.errors == []


@pytest.mark.unit
def test_correct_handler_using_re_import_passes() -> None:
    """A correct handler that genuinely uses `re` must pass the sandbox."""
    fixtures = derive_semantic_fixtures(_CAMEL_TASK)
    result = evaluate_handler_semantics(_CORRECT_CAMEL_USED_IMPORT, fixtures)
    assert result.checked is True
    assert result.passed is True, result.errors


@pytest.mark.unit
@pytest.mark.parametrize(
    "module",
    ["re", "string", "math", "itertools", "functools", "collections", "json"],
)
def test_safe_stdlib_modules_are_importable(module: str) -> None:
    """The standard safe set imports cleanly inside the sandbox."""
    handler = (
        f"import {module}\n\n"
        "def handle(input_data):\n"
        "    text = input_data.get('text', '')\n"
        "    parts = text.strip().split('_')\n"
        "    camel = parts[0].lower() + ''.join(p.capitalize() for p in parts[1:])\n"
        "    return {'camel_case': camel, 'changed': camel != text}\n"
    )
    fixtures = derive_semantic_fixtures(_CAMEL_TASK)
    result = evaluate_handler_semantics(handler, fixtures)
    assert result.checked is True
    assert result.passed is True, result.errors


# ---------------------------------------------------------------------------
# Defect 1 — the I/O prohibition must survive: importing an I/O module fails.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "module", ["os", "sys", "io", "socket", "subprocess", "pathlib"]
)
def test_io_modules_remain_blocked(module: str) -> None:
    """Reaching for an I/O / system module is still a semantic failure."""
    handler = (
        f"import {module}\n\n"
        "def handle(input_data):\n"
        "    return {'camel_case': input_data.get('text', '')}\n"
    )
    fixtures = derive_semantic_fixtures(_CAMEL_TASK)
    result = evaluate_handler_semantics(handler, fixtures)
    assert result.checked is True
    assert result.passed is False
    assert any("raised" in e for e in result.errors)


@pytest.mark.unit
def test_open_builtin_remains_blocked() -> None:
    """OMN-13166 invariant: a handler reaching `open()` still fails the sandbox."""
    io_handler = """\
def handle(input_data):
    with open("/etc/passwd") as fh:
        return {"camel_case": fh.read()}
"""
    fixtures = derive_semantic_fixtures(_CAMEL_TASK)
    result = evaluate_handler_semantics(io_handler, fixtures)
    assert result.checked is True
    assert result.passed is False
    assert any("raised" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Defect 2: no fixtures from loose substring matches on structured tasks.
# ---------------------------------------------------------------------------

# Exact C1 task from the dev matrix (C1-COMPLEX-JSONNORM-EXEMPLAR/request.txt).
_C1_JSON_NORMALIZE_TASK = (
    "Generate an ONEX compute node named postfix_json_normalizer_c1_dev that "
    "accepts a JSON object with optional ticket_id and title fields, normalizes "
    "ticket_id to uppercase OMN-123 form when possible, trims title, and returns "
    "ticket_id, title, and valid boolean."
)


@pytest.mark.unit
def test_structured_task_does_not_derive_whole_string_uppercase_fixtures() -> None:
    """The json-normalizer task must NOT match the bare `uppercase` substring.

    Its real invariant (structured ticket-id normalization, multi-field output)
    is not a known whole-string transform, so the behavioral check must be
    inconclusive (empty fixtures) rather than testing the wrong invariant.
    """
    fixtures = derive_semantic_fixtures(_C1_JSON_NORMALIZE_TASK)
    assert fixtures == [], (
        "structured multi-field task must not derive whole-string uppercase "
        f"fixtures, got: {[(f.transform, f.expected_value) for f in fixtures]}"
    )


@pytest.mark.unit
def test_genuine_whole_string_uppercase_still_derives_fixtures() -> None:
    """A real whole-string uppercase task still derives uppercase fixtures."""
    fixtures = derive_semantic_fixtures(
        "Generate a node that converts the input text to uppercase."
    )
    assert fixtures, "a genuine whole-string uppercase task must derive fixtures"
    assert all(f.transform == "uppercase" for f in fixtures)


@pytest.mark.unit
def test_genuine_whole_string_lowercase_still_derives_fixtures() -> None:
    fixtures = derive_semantic_fixtures(
        "Generate a node that converts the input text to lowercase."
    )
    assert fixtures, "a genuine whole-string lowercase task must derive fixtures"
    assert all(f.transform == "lowercase" for f in fixtures)


# ---------------------------------------------------------------------------
# OMN-13166 no-regression: behaviorally-wrong output still fails.
# ---------------------------------------------------------------------------

_PASCAL_TASK = (
    "Generate an ONEX compute node named night_probe_pascal_case that accepts "
    "snake_case text, trims it, converts it to PascalCase, and returns "
    "pascal_case plus changed boolean."
)

# Whitespace-splitting impostor (the exact OMN-13166 false-green handler).
_GATE_ZERO_IMPOSTOR = """\
def handle(input_data):
    text = input_data.get("text", "")
    trimmed = text.strip()
    original_case = trimmed.lower()
    pascal_case = "".join(word.capitalize() for word in original_case.split())
    changed = pascal_case != text
    return {"pascal_case": pascal_case, "changed": changed}
"""


@pytest.mark.unit
def test_behaviorally_wrong_handler_still_fails_even_with_import() -> None:
    """OMN-13166 no-regression: a wrong handler is rejected even if imports work."""
    wrong_with_import = "import re\n\n" + _GATE_ZERO_IMPOSTOR
    fixtures = derive_semantic_fixtures(_PASCAL_TASK)
    result = evaluate_handler_semantics(wrong_with_import, fixtures)
    assert result.checked is True
    assert result.passed is False
    assert any("snake_to_pascal" in e for e in result.errors)
