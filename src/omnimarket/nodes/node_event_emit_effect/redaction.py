# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Contract-resolved capture redaction for the emit seam (OMN-17209).

The posture lives in ``contracts/capture_redaction.yaml``. This module is the
resolver for it and holds **no policy of its own**: no field name, no topic
name, no secret pattern and no tool name appears here as a Python literal.
``test_capture_redaction_contract.py::test_no_python_side_pattern_constants``
scans this module's source and fails if one does -- OMN-17209's DoD probe 7,
"a committed-recipe gate rejects adding a regex to a Python constant as a
substitute for a contract entry."

Where this sits
---------------
``handler_event_emit_effect._build_messages`` enriches once, then applies each
fan-out rule's named transform per target topic. ``redact_capture`` is one of
those named transforms, so redaction is declared **per topic** in the event
registry (``fan_out[].transform``) and resolved here -- the OMN-16019 seam,
not a second transform beside it.

It is registered in BOTH ``enrichment.TRANSFORM_REGISTRY`` (this node) and
``node_emit_daemon.event_registry.TRANSFORM_REGISTRY`` (the legacy daemon),
pointing at this one callable. That is deliberate: the OMN-16048 parity bar is
byte-identical output across both paths for all 62 event types, so a
node-only redaction layer would show up as a parity break rather than as a
control. The daemon importing this module is the safe direction -- the daemon
is deleted under R5 (OMN-15974), which removes the import with it, whereas the
reverse dependency is the one this package forbids.

Determinism
-----------
``capture_hashed`` is sha256 over the value's canonical JSON form, unsalted.
Unsalted is a requirement, not an oversight: a salt makes the record
unreplayable, and deterministic replay is the doctrine's proof surface. The
consequence is that a hash of a LOW-entropy value is brute-forceable, which is
why hashing is the fail-closed default for *unclassified* fields and never the
protection for a field known to hold a secret -- those are ``never_capture``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_event_emit_effect.errors import (
    MalformedRedactionContractError,
    UngovernedTopicError,
)

JsonDict = dict[str, object]


class EnumCaptureClass(StrEnum):
    """The four capture classes the contract may assign to a field.

    Re-declared here rather than imported so the emit seam keeps its
    stdlib-only import cost -- OMN-17224 measured a 31.08s-of-31.65s
    Pydantic import chain on this path and moved the hook off it.
    """

    CAPTURE_VERBATIM = "capture_verbatim"
    CAPTURE_HASHED = "capture_hashed"
    CAPTURE_SHAPE_ONLY = "capture_shape_only"
    NEVER_CAPTURE = "never_capture"


class EnumRedactionState(StrEnum):
    """Redaction state stamped on every governed record.

    Values mirror ``omnibase_core``'s ``EnumArtifactRedactionState``
    (OMN-13152) exactly; ``test_redaction_state_values_match_omnibase_core``
    asserts the parity by importing core in the TEST, so the runtime path
    stays free of that import (OMN-17224, as for ``EnumCaptureClass``).
    """

    RAW = "raw"
    REDACTED = "redacted"
    RESTRICTED = "restricted"
    SECRET_DETECTED = "secret_detected"  # pragma: allowlist secret


@dataclass(frozen=True)
class OutputClass:
    """One always-hashed output class: hashed for what it is, not what it says."""

    name: str
    tool_names: frozenset[str]
    command_pattern: re.Pattern[str]


@dataclass(frozen=True)
class DerivedField:
    """A field computed from a source field before that source is redacted.

    Exists because the transform this contract replaces on
    ``prompt-submitted`` (``strip_prompt``) derived ``prompt_length`` from the
    prompt it dropped. Redacting a field is the point; silently losing the
    aggregate that survived the redaction is a regression, so the derivation
    is declared rather than dropped.
    """

    target: str
    source: str
    derive: str


@dataclass(frozen=True)
class TopicPolicy:
    """The declared per-field capture classes for one governed topic."""

    topic: str
    fields: dict[str, EnumCaptureClass]
    derived: tuple[DerivedField, ...] = ()


@dataclass(frozen=True)
class RedactionContract:
    """The whole resolved contract."""

    default_field_class: EnumCaptureClass
    output_classes: tuple[OutputClass, ...]
    command_fields: tuple[str, ...]
    tool_name_field: str
    content_fields: frozenset[str]
    secret_patterns: tuple[tuple[str, re.Pattern[str]], ...]
    topics: dict[str, TopicPolicy]
    redaction_state_field: str


def default_contract_path() -> Path:
    """Resolve the contract beside this module, packaging-safe."""
    return Path(__file__).resolve().parent / "contracts" / "capture_redaction.yaml"


def _require(raw: JsonDict, key: str, source: Path) -> Any:
    if key not in raw:
        raise MalformedRedactionContractError(
            source=str(source), detail=f"missing required key {key!r}"
        )
    return raw[key]


def _capture_class(value: Any, *, source: Path, where: str) -> EnumCaptureClass:
    try:
        return EnumCaptureClass(value)
    except ValueError as exc:
        valid = ", ".join(c.value for c in EnumCaptureClass)
        raise MalformedRedactionContractError(
            source=str(source),
            detail=f"{where} declares unknown capture class {value!r} (valid: {valid})",
        ) from exc


def _compile(pattern: Any, *, source: Path, where: str) -> re.Pattern[str]:
    if not isinstance(pattern, str):
        raise MalformedRedactionContractError(
            source=str(source), detail=f"{where} pattern must be a string"
        )
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise MalformedRedactionContractError(
            source=str(source), detail=f"{where} pattern does not compile: {exc}"
        ) from exc


def _parse(raw: Any, *, source: Path) -> RedactionContract:
    if not isinstance(raw, dict):
        raise MalformedRedactionContractError(
            source=str(source), detail="contract YAML must be a mapping"
        )

    default_class = _capture_class(
        _require(raw, "default_field_class", source),
        source=source,
        where="default_field_class",
    )

    output_classes: list[OutputClass] = []
    for entry in _require(raw, "always_hashed_output_classes", source):
        if not isinstance(entry, dict):
            raise MalformedRedactionContractError(
                source=str(source),
                detail="always_hashed_output_classes entries must be mappings",
            )
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise MalformedRedactionContractError(
                source=str(source), detail="output class has no 'name'"
            )
        if not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
            raise MalformedRedactionContractError(
                source=str(source),
                detail=(
                    f"output class {name!r} has no 'reason'. Every always-hashed "
                    "class states why it is one; an unexplained class cannot be "
                    "reviewed or retired."
                ),
            )
        tool_names = entry.get("tool_names")
        if not isinstance(tool_names, list) or not tool_names:
            raise MalformedRedactionContractError(
                source=str(source),
                detail=f"output class {name!r} declares no 'tool_names'",
            )
        output_classes.append(
            OutputClass(
                name=name,
                tool_names=frozenset(str(t) for t in tool_names),
                command_pattern=_compile(
                    _require(entry, "command_pattern", source),
                    source=source,
                    where=f"output class {name!r}",
                ),
            )
        )

    secret_patterns: list[tuple[str, re.Pattern[str]]] = []
    for entry in _require(raw, "secret_patterns", source):
        if not isinstance(entry, dict) or "name" not in entry:
            raise MalformedRedactionContractError(
                source=str(source), detail="secret_patterns entries need a 'name'"
            )
        secret_patterns.append(
            (
                str(entry["name"]),
                _compile(
                    _require(entry, "pattern", source),
                    source=source,
                    where=f"secret pattern {entry['name']!r}",
                ),
            )
        )

    topics: dict[str, TopicPolicy] = {}
    topics_raw = _require(raw, "topics", source)
    if not isinstance(topics_raw, dict) or not topics_raw:
        raise MalformedRedactionContractError(
            source=str(source), detail="'topics' must be a non-empty mapping"
        )
    for topic, policy in topics_raw.items():
        if not isinstance(policy, dict):
            raise MalformedRedactionContractError(
                source=str(source), detail=f"topic {topic!r} policy must be a mapping"
            )
        fields_raw = policy.get("fields")
        if not isinstance(fields_raw, dict) or not fields_raw:
            raise MalformedRedactionContractError(
                source=str(source),
                detail=(
                    f"topic {topic!r} declares no 'fields'. An empty policy means "
                    "'hash everything', which reads identical to a working one."
                ),
            )
        derived: list[DerivedField] = []
        for target, spec in (policy.get("derived_fields") or {}).items():
            if not isinstance(spec, dict) or "from" not in spec:
                raise MalformedRedactionContractError(
                    source=str(source),
                    detail=(
                        f"topic {topic!r} derived field {target!r} needs a "
                        "'from' source field"
                    ),
                )
            derive = spec.get("derive")
            if derive not in _DERIVATIONS:
                raise MalformedRedactionContractError(
                    source=str(source),
                    detail=(
                        f"topic {topic!r} derived field {target!r} declares "
                        f"unknown derivation {derive!r} (valid: "
                        f"{', '.join(sorted(_DERIVATIONS))})"
                    ),
                )
            derived.append(
                DerivedField(
                    target=str(target), source=str(spec["from"]), derive=str(derive)
                )
            )

        topics[str(topic)] = TopicPolicy(
            topic=str(topic),
            fields={
                str(f): _capture_class(
                    c, source=source, where=f"topic {topic!r} field {f!r}"
                )
                for f, c in fields_raw.items()
            },
            derived=tuple(derived),
        )

    state_field = _require(raw, "redaction_state_field", source)
    if not isinstance(state_field, str) or not state_field:
        raise MalformedRedactionContractError(
            source=str(source),
            detail="redaction_state_field must be a non-empty string",
        )

    return RedactionContract(
        default_field_class=default_class,
        output_classes=tuple(output_classes),
        command_fields=tuple(str(f) for f in _require(raw, "command_fields", source)),
        tool_name_field=str(_require(raw, "tool_name_field", source)),
        content_fields=frozenset(
            str(f) for f in _require(raw, "content_fields", source)
        ),
        secret_patterns=tuple(secret_patterns),
        topics=topics,
        redaction_state_field=state_field,
    )


@lru_cache(maxsize=4)
def _load(path_str: str) -> RedactionContract:
    path = Path(path_str)
    if not path.is_file():
        raise MalformedRedactionContractError(
            source=path_str, detail="capture redaction contract not found"
        )
    with path.open(encoding="utf-8") as handle:
        return _parse(yaml.safe_load(handle), source=path)


def load_contract(path: Path | None = None) -> RedactionContract:
    """Load and cache the capture redaction contract."""
    return _load(str(path if path is not None else default_contract_path()))


# ---------------------------------------------------------------------------
# Value operations
# ---------------------------------------------------------------------------


#: The closed set of derivations a contract may name. A derivation is an
#: AGGREGATE over a value being redacted -- never a projection of its content,
#: which would be a way to smuggle content past the capture class.
_DERIVATIONS: dict[str, Any] = {
    "length": lambda value: (
        len(value) if isinstance(value, str | bytes | list | dict) else 0
    ),
}


def _canonical(value: Any) -> str:
    """Canonical JSON form -- the hash input, and the thing replay reproduces."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def hash_value(value: Any) -> str:
    """``sha256:<64 hex>`` over the value's canonical JSON form."""
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def shape_of(value: Any) -> JsonDict:
    """Type + size, with no content and no hash."""
    shape: JsonDict = {"type": type(value).__name__}
    if isinstance(value, str | bytes | list | tuple | dict):
        shape["length"] = len(value)
    return shape


def _matches_secret(value: Any, contract: RedactionContract) -> str | None:
    if not isinstance(value, str):
        return None
    for name, pattern in contract.secret_patterns:
        if pattern.search(value):
            return name
    return None


def _matched_output_class(
    payload: JsonDict, contract: RedactionContract
) -> OutputClass | None:
    """Return the first always-hashed class this record belongs to, if any.

    Matching is on the record's declared tool-name field plus any of the
    declared command fields. It never inspects the OUTPUT: DoD probe 4
    requires an SSM result carrying no secret-shaped text at all to be hashed
    anyway, so a class that consulted the output would be a pattern match
    wearing a class's name.
    """
    tool = payload.get(contract.tool_name_field)
    tool_name = tool if isinstance(tool, str) else ""
    candidates = [
        payload[field] for field in contract.command_fields if field in payload
    ]
    for output_class in contract.output_classes:
        if tool_name not in output_class.tool_names:
            continue
        for candidate in candidates:
            text = candidate if isinstance(candidate, str) else _canonical(candidate)
            if output_class.command_pattern.search(text):
                return output_class
    return None


def redact_capture(
    payload: JsonDict, topic: str, *, contract_path: Path | None = None
) -> JsonDict:
    """Apply the topic's declared capture policy and stamp the redaction state.

    Order, and why:

    1. Resolve the topic's policy. A topic whose fan-out rule names this
       transform but which the contract does not govern is a hard refusal --
       falling back to "hash everything" would publish a record that looks
       redacted while nobody has ever reviewed what it carries.
    2. Match the always-hashed output classes on tool name + command shape.
       A match forces every declared content field to ``capture_hashed``,
       overriding its per-field class. Class beats field, always.
    3. Apply each field's class, with the contract's fail-closed default for
       any field nobody declared.
    4. Run the secret scrub over what survived as verbatim. A hit hashes the
       value and escalates the record's state to ``secret_detected``.
    5. Stamp the state field. Always -- a record with no state is refused
       downstream, so an unstamped record must not exist.

    Raises:
        UngovernedTopicError: the topic names this transform but declares no
            field policy.
    """
    contract = load_contract(contract_path)
    policy = contract.topics.get(topic)
    if policy is None:
        raise UngovernedTopicError(
            topic=topic,
            governed=tuple(sorted(contract.topics)),
            contract_path=str(
                contract_path if contract_path is not None else default_contract_path()
            ),
        )

    forced = _matched_output_class(payload, contract)
    state = EnumRedactionState.RAW
    result: JsonDict = {}

    # Derivations read the SOURCE before it is redacted, and only fill a
    # target the producer did not already supply.
    derived_values: JsonDict = {}
    for rule in policy.derived:
        if rule.target in payload or rule.source not in payload:
            continue
        derived_values[rule.target] = _DERIVATIONS[rule.derive](payload[rule.source])

    for field, value in list(payload.items()) + list(derived_values.items()):
        if field == contract.redaction_state_field:
            # A producer does not get to declare its own posture.
            state = max(state, EnumRedactionState.REDACTED, key=_state_rank)
            continue

        capture_class = policy.fields.get(field, contract.default_field_class)
        if forced is not None and field in contract.content_fields:
            capture_class = EnumCaptureClass.CAPTURE_HASHED

        if capture_class is EnumCaptureClass.NEVER_CAPTURE:
            state = max(state, EnumRedactionState.REDACTED, key=_state_rank)
            continue
        if capture_class is EnumCaptureClass.CAPTURE_HASHED:
            result[field] = hash_value(value)
            state = max(state, EnumRedactionState.REDACTED, key=_state_rank)
            continue
        if capture_class is EnumCaptureClass.CAPTURE_SHAPE_ONLY:
            result[field] = shape_of(value)
            state = max(state, EnumRedactionState.REDACTED, key=_state_rank)
            continue

        # capture_verbatim -- still subject to the scrub.
        if _matches_secret(value, contract) is not None:
            result[field] = hash_value(value)
            state = EnumRedactionState.SECRET_DETECTED
        else:
            result[field] = value

    result[contract.redaction_state_field] = state.value
    return result


_STATE_RANK: dict[str, int] = {
    EnumRedactionState.RAW.value: 0,
    EnumRedactionState.RESTRICTED.value: 1,
    EnumRedactionState.REDACTED.value: 2,
    EnumRedactionState.SECRET_DETECTED.value: 3,
}


def _state_rank(state: EnumRedactionState) -> int:
    return _STATE_RANK[state.value]


__all__: list[str] = [
    "DerivedField",
    "EnumCaptureClass",
    "EnumRedactionState",
    "OutputClass",
    "RedactionContract",
    "TopicPolicy",
    "default_contract_path",
    "hash_value",
    "load_contract",
    "redact_capture",
    "shape_of",
]
