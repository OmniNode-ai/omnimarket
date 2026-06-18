# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Semantic validation for generated compute-node handlers (OMN-13166).

Contract/schema validation (``_validate_generation`` in
``handler_generation_consumer``) only proves that the generated artifact is
*shaped* like an ONEX node: the YAML carries the required fields, the handler
parses and exposes ``handle()``, and there are no hardcoded paths/topics. It
says nothing about whether the handler *does the requested transformation*.

The 2026-06-16 gate-zero stability SEA cell exposed the gap: the model emitted a
syntactically valid handler that split on whitespace instead of underscores, so
``hello_world`` produced ``Hello_world`` rather than ``HelloWorld`` — yet the
run was projected as ``contract_passed=true``.

This module closes that gap with a *behavioral* layer that is independent of
schema validation:

1. ``derive_semantic_fixtures`` reads the task description, recognises a known
   transformation invariant (snake_case -> PascalCase, trimming, casing,
   reversal), and synthesises concrete input/expected fixtures for it. When no
   invariant is derivable, it returns an empty list (the check is *not
   applicable* — that is honest, not a pass).
2. ``evaluate_handler_semantics`` executes the generated ``handle()`` against
   each fixture inside an isolated namespace whose builtins are restricted to a
   pure, deterministic, I/O-free subset, and compares the produced value with
   the expected value.

The handler is stateless and deterministic by archetype (NodeCompute), so the
fixtures are deterministic and replay-safe. No network, filesystem, env, time,
or randomness is reachable from the executed code: the restricted builtins omit
``open``, ``eval``, ``exec``, ``input``, etc., so an attempt to reach I/O raises
``NameError`` inside the sandbox and is reported as a semantic failure rather
than escaping it.

OMN-13217: the sandbox originally omitted ``__import__`` entirely, which
false-REJECTed behaviorally-correct handlers that import a safe stdlib module
such as ``re`` (used OR merely declared). It now exposes a *controlled*
``__import__`` that admits a curated, pure, deterministic, I/O-free stdlib
allowlist (``re``, ``string``, ``math``, ``itertools``, ``functools``,
``collections``, ``json``, ...) and raises ``ImportError`` for anything else —
so a correct ``import re`` passes while ``import os`` / ``import socket`` /
``import subprocess`` remain a semantic failure (the I/O prohibition is intact).
"""

from __future__ import annotations

import importlib
from types import ModuleType

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ModelSemanticFixture",
    "ModelSemanticResult",
    "derive_semantic_fixtures",
    "evaluate_handler_semantics",
]


class ModelSemanticFixture(BaseModel):
    """A single synthesized input/expected pair for a transformation task.

    The model under test may legitimately name its primary output field any of
    several plausible names (``pascal_case``, ``result``, ``output`` ...), so the
    fixture carries a SET of acceptable key names plus the single ``expected_value``
    they must hold — exactly one of those keys must be present with that value.
    ``required_fields`` pins any additional fields that must also be exact (e.g.
    the ``changed`` boolean), keyed by name.

    ``transform`` names the derived invariant (e.g. ``snake_to_pascal``) for
    evidence/debugging.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    transform: str = Field(
        description="Identifier of the derived transformation invariant"
    )
    input_data: dict[str, object] = Field(
        description="Input payload passed to the generated handle(input_data)"
    )
    output_keys: tuple[str, ...] = Field(
        description=(
            "Acceptable names for the primary transformed-value output field. "
            "At least one must be present in the handler's return with "
            "expected_value."
        )
    )
    expected_value: object = Field(
        description="The correct transformed value the primary output field must hold"
    )
    required_fields: dict[str, object] = Field(
        default_factory=dict,
        description=(
            "Additional output fields that must be present with these exact "
            "values (e.g. a 'changed' boolean). Empty when none are required."
        ),
    )


class ModelSemanticResult(BaseModel):
    """Outcome of behavioral validation, kept separate from contract validation.

    Attributes:
        checked: Whether any semantic fixture was applicable. ``False`` means no
            transformation invariant could be derived from the task — the result
            is *inconclusive*, NOT a pass. Callers must treat ``checked=False``
            as "no behavioral evidence", not "behavior is correct".
        passed: ``True`` only when ``checked`` is ``True`` AND the handler
            produced the expected output for every fixture. Always ``False`` when
            ``checked`` is ``False``.
        fixtures_total: Number of fixtures evaluated.
        fixtures_passed: Number of fixtures whose actual output matched expected.
        errors: Human-readable mismatch / execution-failure descriptions.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    checked: bool = Field(default=False)
    passed: bool = Field(default=False)
    fixtures_total: int = Field(default=0, ge=0)
    fixtures_passed: int = Field(default=0, ge=0)
    errors: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Restricted execution sandbox
# ---------------------------------------------------------------------------

# Curated stdlib modules that are pure, deterministic, and I/O-free. Generated
# string-transformation handlers reach for these (most often ``re``). Modules
# that touch the filesystem / network / env / clock / randomness (``os``,
# ``sys``, ``io``, ``socket``, ``subprocess``, ``pathlib``, ``time``,
# ``random``, ...) are deliberately EXCLUDED, so importing one stays a semantic
# failure and the I/O prohibition that catches malicious handlers is intact.
_SAFE_STDLIB_MODULES: frozenset[str] = frozenset(
    {
        "re",
        "string",
        "math",
        "itertools",
        "functools",
        "collections",
        "collections.abc",
        "json",
        "decimal",
        "fractions",
        "textwrap",
        "unicodedata",
        "operator",
        "typing",
        "dataclasses",
        "enum",
        "numbers",
    }
)


def _safe_import(
    name: str,
    globals_: dict[str, object] | None = None,
    locals_: dict[str, object] | None = None,
    fromlist: tuple[str, ...] = (),
    level: int = 0,
) -> ModuleType:
    """A ``__import__`` that admits only the curated safe stdlib allowlist.

    Mirrors the CPython ``__import__`` signature so ``import re`` and
    ``from collections import OrderedDict`` both work. Relative imports
    (``level > 0``) and any module outside ``_SAFE_STDLIB_MODULES`` raise
    ``ImportError`` — surfaced by the caller as a semantic failure, never an
    escape from the sandbox.
    """
    if level != 0:
        raise ImportError("relative imports are not permitted in the semantic sandbox")
    top = name.split(".", 1)[0]
    if name not in _SAFE_STDLIB_MODULES and top not in _SAFE_STDLIB_MODULES:
        raise ImportError(
            f"import of {name!r} is not permitted in the semantic sandbox"
        )
    return importlib.import_module(name)


# A pure, deterministic, I/O-free builtin subset. Generated transformation
# handlers only need string/sequence primitives plus a guarded ``__import__``
# (OMN-13217) limited to the safe stdlib allowlist above. Notably ABSENT: open,
# eval, exec, compile, input, globals, locals, vars, getattr, setattr, delattr
# — so reaching network/filesystem/env/time/random from inside raises NameError
# (or ImportError for a disallowed module), which surfaces as a semantic failure
# (not an escape).
_SAFE_BUILTINS: dict[str, object] = {
    "__import__": _safe_import,
    "abs": abs,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "filter": filter,
    "float": float,
    "int": int,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "range": range,
    "reversed": reversed,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
}


def _execute_handler(handler_source: str, input_data: dict[str, object]) -> object:
    """Compile + run the generated handler's handle() in a restricted namespace.

    Raises whatever the handler raises (or a NameError when it reaches for a
    disallowed builtin). The caller converts a raised exception into a recorded
    semantic failure — it never propagates out of the validation pass.
    """
    namespace: dict[str, object] = {"__builtins__": _SAFE_BUILTINS}
    compiled = compile(handler_source, filename="<generated-handler>", mode="exec")
    exec(compiled, namespace)
    handle = namespace.get("handle")
    if not callable(handle):
        raise ValueError("generated source defines no callable handle()")
    return handle(input_data)


def evaluate_handler_semantics(
    handler_source: str,
    fixtures: list[ModelSemanticFixture],
) -> ModelSemanticResult:
    """Run the generated handler against synthesized fixtures and compare output.

    When ``fixtures`` is empty the check is inconclusive: returns ``checked=False,
    passed=False`` — no behavioral evidence was produced, so the caller must not
    claim semantic success.
    """
    if not fixtures:
        return ModelSemanticResult(checked=False, passed=False)

    errors: list[str] = []
    fixtures_passed = 0

    for fixture in fixtures:
        try:
            actual = _execute_handler(handler_source, dict(fixture.input_data))
        except Exception as exc:
            errors.append(
                f"semantic: handler raised {type(exc).__name__} on input "
                f"{fixture.input_data!r} ({fixture.transform}): {exc}"
            )
            continue

        if _output_matches(actual, fixture):
            fixtures_passed += 1
        else:
            errors.append(
                f"semantic: {fixture.transform} expected "
                f"{fixture.expected_value!r} on one of {fixture.output_keys} "
                f"(plus {fixture.required_fields}) for input "
                f"{fixture.input_data!r}, got {actual!r}"
            )

    passed = fixtures_passed == len(fixtures)
    return ModelSemanticResult(
        checked=True,
        passed=passed,
        fixtures_total=len(fixtures),
        fixtures_passed=fixtures_passed,
        errors=errors,
    )


def _output_matches(actual: object, fixture: ModelSemanticFixture) -> bool:
    """The handler output must carry the correct transformed value + required fields.

    A handler legitimately names its primary output field any of several plausible
    names, so exactly one of ``fixture.output_keys`` must be present with
    ``fixture.expected_value``. Each entry in ``fixture.required_fields`` (e.g. a
    ``changed`` boolean) must also be present with its exact value. Extra,
    unrelated keys are tolerated — they do not make the invariant wrong.
    """
    if not isinstance(actual, dict):
        return False
    primary_ok = any(
        key in actual and actual[key] == fixture.expected_value
        for key in fixture.output_keys
    )
    if not primary_ok:
        return False
    for key, value in fixture.required_fields.items():
        if key not in actual or actual[key] != value:
            return False
    return True


# ---------------------------------------------------------------------------
# Fixture derivation from the natural-language task description
# ---------------------------------------------------------------------------

# Each fixture probes one canonical sample so a plainly-wrong invariant fails
# loudly. The samples deliberately include a discriminating case (e.g. an input
# with underscores AND whitespace) so a whitespace-splitting impostor diverges
# from the underscore-splitting requirement.
_PASCAL_KEYS = ("pascal_case", "pascalcase", "result", "output")
_CAMEL_KEYS = ("camel_case", "camelcase", "result", "output")
_REVERSED_KEYS = ("reversed", "result", "output")
_UPPER_KEYS = ("upper", "uppercase", "result", "output")
_LOWER_KEYS = ("lower", "lowercase", "result", "output")
_INPUT_KEYS = ("text", "value", "input", "input_text", "string")


def _wants(text: str, *needles: str) -> bool:
    return any(n in text for n in needles)


# OMN-13217: signals that a task is a STRUCTURED transform (an object with
# multiple fields, or normalization of a specific field into a specific form)
# rather than a single whole-string transform. A casing word that appears inside
# such a task ("normalizes ticket_id to uppercase OMN-123 form") is a qualifier,
# NOT the invariant — deriving plain whole-string uppercase fixtures
# (``hello`` -> ``HELLO``) for it judges the handler against the wrong transform.
_STRUCTURED_TASK_SIGNALS: tuple[str, ...] = (
    "json object",
    "json payload",
    "object with",
    "fields",
    "ticket_id",
    "ticket id",
    "schema",
    "dictionary",
    "dict with",
    "nested",
)


def _is_structured_task(text: str) -> bool:
    """True when the task shapes a multi-field / object transform, not a string one.

    Whole-string casing/reversal invariants (uppercase, lowercase, reverse) are
    matched by loose substrings, so they must NOT fire on a structured task whose
    casing word is only a qualifier. A task that returns several named fields, or
    operates on a JSON object / specific field, is structured: its invariant is
    not a single whole-string transform and the behavioral check is inconclusive
    rather than guessed against the wrong fixtures.
    """
    if _wants(text, *_STRUCTURED_TASK_SIGNALS):
        return True
    # "returns a, b, and c" / "returns x plus y" — more than one named output.
    if "return" in text and _wants(text, ", and ", " plus ", " and returns"):
        return True
    return False


def derive_semantic_fixtures(task_description: str) -> list[ModelSemanticFixture]:
    """Derive input/expected fixtures from a transformation task description.

    Recognises a small, high-confidence set of WHOLE-STRING transformation
    invariants. Returns an empty list when the task does not match a known
    invariant — the behavioral check is then inconclusive rather than guessed.

    OMN-13217: loose-substring matches on structured/multi-field tasks are
    rejected. A casing/reversal keyword that is merely a qualifier inside a
    structured normalization task (e.g. "normalizes ticket_id to uppercase
    OMN-123 form, ... and returns ticket_id, title, and valid boolean") does NOT
    derive plain whole-string fixtures — those would test the wrong invariant.
    The cased/compound invariants (snake->Pascal, snake->camel) require their
    strong keyword AND snake_case framing; the bare upper/lower/reverse branches
    additionally require a single-string task shape.

    The input is keyed by every plausible input field name the model might read
    (the handler picks one via ``input_data.get(...)``). The gate-zero task uses
    ``text`` -> ``{pascal_case, changed}``, covered exactly by the snake_to_pascal
    branch. A ``changed`` boolean is required only when the task asks for it.
    """
    text = task_description.lower()
    wants_changed = _wants(text, "changed", "modified")

    # snake_case -> PascalCase (the gate-zero stability invariant). Requires the
    # strong "pascal" keyword AND snake_case framing so it is not a loose match.
    if _wants(text, "pascalcase", "pascal case", "pascal_case", "pascal") and _wants(
        text, "snake_case", "snake case", "snake"
    ):
        return _snake_to_pascal_fixtures(wants_changed)

    # snake_case -> camelCase. Same strong-keyword + snake_case requirement.
    if _wants(text, "camelcase", "camel case", "camel_case", "camel") and _wants(
        text, "snake_case", "snake case", "snake"
    ):
        return _snake_to_camel_fixtures(wants_changed)

    # Whole-string invariants below are loose-substring-prone, so they never fire
    # on a structured/multi-field task (OMN-13217).
    if _is_structured_task(text):
        return []

    # Reverse a string.
    if _wants(text, "revers"):
        return _reverse_fixtures()

    # Uppercase / lowercase conversion (only when not part of snake/pascal/camel
    # phrasing, which is handled above).
    if _wants(text, "uppercase", "upper case", "to upper"):
        return _upper_fixtures()
    if _wants(text, "lowercase", "lower case", "to lower"):
        return _lower_fixtures()

    return []


def _input_payload(value: str) -> dict[str, object]:
    """Populate every plausible input field name so the handler reads its choice."""
    return dict.fromkeys(_INPUT_KEYS, value)


def _changed_field(
    raw: str, transformed: str, wants_changed: bool
) -> dict[str, object]:
    return {"changed": transformed != raw} if wants_changed else {}


def _snake_to_pascal_fixtures(wants_changed: bool) -> list[ModelSemanticFixture]:
    samples = [
        ("hello_world", "HelloWorld"),
        ("  multi_word_value  ", "MultiWordValue"),
    ]
    return [
        ModelSemanticFixture(
            transform="snake_to_pascal",
            input_data=_input_payload(raw),
            output_keys=_PASCAL_KEYS,
            expected_value=pascal,
            required_fields=_changed_field(raw, pascal, wants_changed),
        )
        for raw, pascal in samples
    ]


def _snake_to_camel_fixtures(wants_changed: bool) -> list[ModelSemanticFixture]:
    samples = [
        ("hello_world", "helloWorld"),
        ("  multi_word_value  ", "multiWordValue"),
    ]
    return [
        ModelSemanticFixture(
            transform="snake_to_camel",
            input_data=_input_payload(raw),
            output_keys=_CAMEL_KEYS,
            expected_value=camel,
            required_fields=_changed_field(raw, camel, wants_changed),
        )
        for raw, camel in samples
    ]


def _reverse_fixtures() -> list[ModelSemanticFixture]:
    samples = [("abc", "cba"), ("OmniNode", "edoNinmO")]
    return [
        ModelSemanticFixture(
            transform="reverse",
            input_data=_input_payload(raw),
            output_keys=_REVERSED_KEYS,
            expected_value=rev,
        )
        for raw, rev in samples
    ]


def _upper_fixtures() -> list[ModelSemanticFixture]:
    samples = [("hello", "HELLO"), ("MixedCase", "MIXEDCASE")]
    return [
        ModelSemanticFixture(
            transform="uppercase",
            input_data=_input_payload(raw),
            output_keys=_UPPER_KEYS,
            expected_value=up,
        )
        for raw, up in samples
    ]


def _lower_fixtures() -> list[ModelSemanticFixture]:
    samples = [("HELLO", "hello"), ("MixedCase", "mixedcase")]
    return [
        ModelSemanticFixture(
            transform="lowercase",
            input_data=_input_payload(raw),
            output_keys=_LOWER_KEYS,
            expected_value=lo,
        )
        for raw, lo in samples
    ]
