# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Import ONE source tree's wire models and answer ONE question about them.

This module is the subprocess half of ``check_wire_compatibility.py`` and it
exists for a reason that cannot be engineered away: **two versions of the same
package cannot both be imported into one interpreter.** The gate has to hold
the WORKING TREE's ``ModelDelegateSkillRequest`` and the LAST RELEASED
``ModelDelegateSkillRequest`` at the same time, and ``sys.modules`` has exactly
one slot named ``omnimarket``. So each tree is loaded in its own process, with
its own ``sys.path[0]``, and the two processes exchange JSON.

Two modes, both narrow on purpose:

``describe``
    Import every ``BaseModel`` DEFINED IN the named module (re-exports are
    skipped -- a class whose ``__module__`` points elsewhere belongs to the
    package that defines it, and grading it here would blame this repository
    for ``omnibase_core``'s contract) and report, per class, the keys it can
    emit on the wire and its ``extra`` policy.

``replay``
    Import the same module and push a caller-supplied payload through each
    class's real ``model_validate``. This is a genuine decode at the real
    boundary, not a field-name set comparison: the released model's own
    validators run, and its own ``extra="forbid"`` raises its own error.

The probe never decides anything. It reports what the interpreter did, and the
parent gate grades it. That split is what lets the parent fail CLOSED on a
probe that crashed, rather than reading a crash as a clean bill of health.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any

#: The pydantic error types the gate treats as WIRE-SHAPE findings.
#:
#: ``extra_forbidden`` is the OMN-18852 class verbatim: the producer emits a
#: key the released consumer's model refuses outright at the decode boundary.
#: The live terminal read "published_at: Extra inputs are not permitted".
#:
#: ``missing`` is the mirror-image defect and is just as breaking: the producer
#: STOPPED emitting a key the released consumer still requires. A deployed
#: consumer refuses that payload for the same reason at the same boundary.
#:
#: Every other pydantic error type is deliberately NOT a finding here -- see
#: ``PROBE_PLACEHOLDER`` for why a type error is an artifact of this probe
#: rather than a fact about the wire.
WIRE_SHAPE_ERROR_TYPES: frozenset[str] = frozenset({"extra_forbidden", "missing"})

#: The value every field in a replay payload carries.
#:
#: The gate asks a SHAPE question -- "can the released consumer accept the set
#: of keys this producer can emit" -- and deliberately does not ask a VALUE
#: question. Synthesising a type-correct value for every annotation in the wire
#: package would mean reimplementing a factory library, and every gap in that
#: synthesiser would surface as a red gate on a change that broke nothing,
#: which is how a gate gets switched off.
#:
#: So every key carries one placeholder string, and the parent grades ONLY the
#: two error types in ``WIRE_SHAPE_ERROR_TYPES``. A ``string_type`` or
#: ``datetime_parsing`` error against this placeholder says nothing about the
#: wire and is discarded. The two types that ARE graded are exactly the two
#: that a placeholder cannot manufacture: ``extra_forbidden`` fires on the KEY
#: being present and ``missing`` on the KEY being absent, and neither looks at
#: the value at all.
PROBE_PLACEHOLDER = "__wire_compat_probe__"


def _emitted_key(field_name: str, field_info: Any) -> str:
    """Return the key this field actually appears under on the wire.

    Serialisation alias wins over the plain alias, which wins over the field
    name -- the same precedence pydantic itself applies when dumping. Reading
    the field name here instead would make the gate blind to exactly the
    aliased fields, which are the ones whose wire name is easiest to change by
    accident.
    """
    serialization_alias = getattr(field_info, "serialization_alias", None)
    if serialization_alias:
        return str(serialization_alias)
    alias = getattr(field_info, "alias", None)
    if alias:
        return str(alias)
    return field_name


def _validation_key(field_name: str, field_info: Any) -> str:
    """Return the key this field is accepted under when decoding.

    Distinct from :func:`_emitted_key` because pydantic reads ``alias`` /
    ``validation_alias`` on the way IN and ``serialization_alias`` on the way
    OUT, and a model may set them differently. The replay payload is built from
    the producer's emitted keys and decoded by the consumer's validation keys,
    so the gate has to know both or it will invent a mismatch that the runtime
    does not have.
    """
    validation_alias = getattr(field_info, "validation_alias", None)
    if isinstance(validation_alias, str) and validation_alias:
        return validation_alias
    alias = getattr(field_info, "alias", None)
    if alias:
        return str(alias)
    return field_name


def _own_models(module: Any) -> dict[str, Any]:
    """Return the ``BaseModel`` subclasses this module DEFINES, by class name.

    ``cls.__module__ == module.__name__`` is the whole filter. The wire package
    re-exports a good deal of ``omnibase_core`` -- ``model_budget.py`` is a pure
    re-export shim -- and grading a re-exported class here would report an
    upstream contract change as an omnimarket wire break, on a pull request
    that did not touch it.
    """
    from pydantic import BaseModel

    found: dict[str, Any] = {}
    for name in dir(module):
        candidate = getattr(module, name)
        if not isinstance(candidate, type):
            continue
        if not issubclass(candidate, BaseModel):
            continue
        if candidate is BaseModel:
            continue
        if candidate.__module__ != module.__name__:
            continue
        found[candidate.__name__] = candidate
    return found


def _describe(module: Any) -> dict[str, Any]:
    """Report the wire surface of every model this module defines."""
    described: dict[str, Any] = {}
    for class_name, model_cls in _own_models(module).items():
        fields = model_cls.model_fields
        described[class_name] = {
            "emitted_keys": sorted(
                _emitted_key(name, info) for name, info in fields.items()
            ),
            "validation_keys": sorted(
                _validation_key(name, info) for name, info in fields.items()
            ),
            "extra": str(model_cls.model_config.get("extra") or "ignore"),
        }
    return described


def _replay(module: Any, payloads: dict[str, list[str]]) -> dict[str, Any]:
    """Decode each payload with the model of the same name in this module.

    Returns, per class name, the wire-shape errors its real validator raised.
    A class named in ``payloads`` but absent from this module is reported as
    ``absent`` rather than as clean: to the parent that means "the released
    artifact has no such consumer", which is a different verdict from "the
    released consumer accepted it" and must not collapse into it.
    """
    from pydantic import ValidationError

    models = _own_models(module)
    result: dict[str, Any] = {}
    for class_name, keys in payloads.items():
        model_cls = models.get(class_name)
        if model_cls is None:
            result[class_name] = {"absent": True, "errors": []}
            continue
        payload = dict.fromkeys(keys, PROBE_PLACEHOLDER)
        errors: list[dict[str, str]] = []
        try:
            model_cls.model_validate(payload)
        except ValidationError as exc:
            for error in exc.errors():
                error_type = str(error.get("type", ""))
                if error_type not in WIRE_SHAPE_ERROR_TYPES:
                    continue
                location = error.get("loc") or ()
                errors.append(
                    {
                        "type": error_type,
                        "field": ".".join(str(part) for part in location),
                        "message": str(error.get("msg", "")),
                    }
                )
        result[class_name] = {"absent": False, "errors": errors}
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--src-root",
        required=True,
        help="Directory placed FIRST on sys.path, so its packages win.",
    )
    parser.add_argument("--module", required=True, help="Dotted module to import.")
    parser.add_argument("--mode", required=True, choices=("describe", "replay"))
    parser.add_argument(
        "--payloads",
        default=None,
        help="JSON file mapping class name -> list of wire keys (replay only).",
    )
    parser.add_argument("--out", required=True, help="File to write the JSON answer.")
    args = parser.parse_args(argv)

    # FIRST on sys.path, not appended: an appended root loses to the installed
    # distribution every time, and the probe would then silently describe the
    # working tree while reporting it as the release. That is the exact
    # tree-shaped blindness this gate exists to end, so it must not be possible
    # to reintroduce it by import order.
    sys.path.insert(0, str(Path(args.src_root).resolve()))

    module = importlib.import_module(args.module)
    resolved = Path(getattr(module, "__file__", "") or "").resolve()
    expected_root = Path(args.src_root).resolve()
    if expected_root not in resolved.parents:
        # The interpreter served a DIFFERENT copy of this module than the one
        # asked for -- an editable install or a stale ``sys.modules`` entry
        # winning the race. Reporting that copy's fields as the release would
        # be the worst possible failure: a green gate that compared the tree
        # against itself.
        sys.stderr.write(
            f"probe resolved {args.module} to {resolved}, "
            f"which is not under {expected_root}\n"
        )
        return 2

    if args.mode == "describe":
        answer: dict[str, Any] = _describe(module)
    else:
        if not args.payloads:
            parser.error("--payloads is required in replay mode")
        payloads = json.loads(Path(args.payloads).read_text(encoding="utf-8"))
        answer = _replay(module, payloads)

    Path(args.out).write_text(json.dumps(answer, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
