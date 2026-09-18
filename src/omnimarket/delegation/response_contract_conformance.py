# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Conformance of a model response to a declared JSON-Schema response contract.

OMN-7942. One implementation, shared by the two seams that each need it, in
the same spirit as ``reasoning_preamble`` beside it: the VERIFICATION seam,
where the quality gate decides whether the answer conforms, and the RESPONSE
seam, where the caller's text is decided. A caller that declares a schema and
is handed back prose with the object buried in it has not been served, even
when the gate correctly passed the run.

Splitting these two out of the gate handler is what stops the seams drifting:
the value the gate graded and the value the caller receives are produced by the
same function over the same content.
"""

from __future__ import annotations

import json
from typing import Any

import jsonschema
import jsonschema.validators

__all__ = [
    "SCHEMA_VIOLATION_PREFIX",
    "locate_schema_conforming_json",
    "schema_violation_reasons",
]

SCHEMA_VIOLATION_PREFIX = "SCHEMA_VIOLATION"


def schema_violation_reasons(
    candidate: Any, response_contract: dict[str, object]
) -> list[str]:
    """Return specific per-violation reasons for a JSON-Schema mismatch.

    Uses whichever ``jsonschema`` validator class matches the contract's own
    declared ``$schema`` (falling back to the latest supported draft when the
    contract declares none), so a per-violation reason names the exact
    JSON-pointer path and the exact constraint that failed (e.g. "'action' is a
    required property", "'confidence' is not of type 'number'") instead of a
    single opaque pass/fail bit. Errors are sorted by path for a stable,
    replay-identical ordering. Raises ``jsonschema.exceptions.SchemaError`` when
    ``response_contract`` itself is not a valid JSON Schema -- a caller-authoring
    bug that must surface loudly, never silently pass every candidate.
    """
    validator_cls = jsonschema.validators.validator_for(response_contract)
    validator_cls.check_schema(response_contract)
    validator = validator_cls(response_contract)
    errors = sorted(
        validator.iter_errors(candidate),
        key=lambda error: [str(part) for part in error.path],
    )
    return [
        f"{SCHEMA_VIOLATION_PREFIX}: "
        f"{'.'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
        for error in errors
    ]


def locate_schema_conforming_json(
    content: str, response_contract: dict[str, object]
) -> tuple[object, bool] | None:
    """Find a JSON value inside ``content`` that satisfies ``response_contract``.

    OMN-7942/OMN-18278. Returns ``(candidate, was_embedded)``, or ``None`` when
    no JSON value in the content parses at all.

    Why this exists, measured rather than assumed. With the declared schema now
    conveyed to the model (OMN-7942), the served ``Qwen3.8-27B`` on the lab
    endpoint reads it correctly -- it names both required keys, picks a valid
    enum member, and emits a conforming object. It just emits an UNTAGGED prose
    preamble in front of it, and writes "Ensure JSON only. No markdown. Need
    final only JSON." inside that preamble while doing so. OMN-18278 recorded
    the same self-certifying pattern and concluded, correctly, that prompting
    is not the remedy for it.

    The boundary rules in ``segment_reasoning_preamble`` claim a paired or an
    unpaired think tag. This shape carries neither, so nothing claims it, and
    the contract evaluator parsed from character zero and failed a conforming
    answer as MALFORMED. Three local attempts failed that way per run and the
    router climbed to a metered cloud rung every time.

    This is deliberately NOT a general prose-stripping heuristic, and it is
    confined to the response-contract branch. A declared JSON Schema is what
    makes the search unambiguous: a candidate is accepted only if it both
    parses AND validates, so there is no guessing about which fragment is the
    answer. When several validate, the LAST is taken -- a model that reasons
    about a shape before committing to it emits the draft first and the answer
    last. When none validate, the last one that merely parsed is returned so
    the caller still gets per-field SCHEMA_VIOLATION reasons rather than a bare
    parse error.
    """
    decoder = json.JSONDecoder()
    parsed: list[object] = []
    conforming: list[object] = []
    for index, character in enumerate(content):
        if character not in "{[":
            continue
        try:
            candidate, _ = decoder.raw_decode(content, index)
        except json.JSONDecodeError:
            continue
        parsed.append(candidate)
        if not schema_violation_reasons(candidate, response_contract):
            conforming.append(candidate)
    if conforming:
        return conforming[-1], True
    if parsed:
        return parsed[-1], True
    return None
