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
from collections.abc import Iterator
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
    # OMN-19550: the value crosses with each secret-pattern SPAN replaced in
    # place, for the full-content topic, where hashing a whole field because
    # of one line would discard the content the operator ruled is captured.
    CAPTURE_SCRUBBED = "capture_scrubbed"


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
class ContentPolicy:
    """A topic's size bound for its ``capture_scrubbed`` fields (OMN-19550).

    ``chunk_chars`` bounds one record's scrubbed value and is enforced at the
    fan-out. ``max_content_chars`` bounds one content item before it is
    chunked and is enforced by the producer through
    :func:`plan_content_chunks`. The two field names are where a record says
    that the bound cut it, and how long it was before.
    """

    chunk_chars: int
    max_content_chars: int
    truncated_field: str
    original_field: str


@dataclass(frozen=True)
class ContentPlan:
    """How one content item is published: its chunks and what was cut."""

    chunks: tuple[str, ...]
    original_chars: int
    truncated: bool
    content_sha256: str


@dataclass(frozen=True)
class TopicPolicy:
    """The declared per-field capture classes for one governed topic."""

    topic: str
    fields: dict[str, EnumCaptureClass]
    derived: tuple[DerivedField, ...] = ()
    content_policy: ContentPolicy | None = None


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
    #: The replacement text for one scrubbed span, with ``{name}`` standing for
    #: the pattern's name. ``None`` only in a contract that declares no
    #: ``capture_scrubbed`` field, which the parser enforces.
    scrub_marker: str | None = None


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

        fields = {
            str(f): _capture_class(
                c, source=source, where=f"topic {topic!r} field {f!r}"
            )
            for f, c in fields_raw.items()
        }
        content_policy = _parse_content_policy(
            policy.get("content_policy"), topic=str(topic), source=source
        )
        if (
            EnumCaptureClass.CAPTURE_SCRUBBED in fields.values()
            and content_policy is None
        ):
            raise MalformedRedactionContractError(
                source=str(source),
                detail=(
                    f"topic {topic!r} declares a capture_scrubbed field but no "
                    "content_policy. A scrubbed field crosses with its content, "
                    "so it needs a declared size bound or one record could "
                    "exceed what the broker accepts."
                ),
            )
        topics[str(topic)] = TopicPolicy(
            topic=str(topic),
            fields=fields,
            derived=tuple(derived),
            content_policy=content_policy,
        )

    state_field = _require(raw, "redaction_state_field", source)
    if not isinstance(state_field, str) or not state_field:
        raise MalformedRedactionContractError(
            source=str(source),
            detail="redaction_state_field must be a non-empty string",
        )

    scrub_marker = raw.get("scrub_marker")
    uses_scrub = any(
        EnumCaptureClass.CAPTURE_SCRUBBED in policy.fields.values()
        for policy in topics.values()
    )
    if scrub_marker is not None and (
        not isinstance(scrub_marker, str) or "{name}" not in scrub_marker
    ):
        raise MalformedRedactionContractError(
            source=str(source),
            detail="scrub_marker must be a string containing '{name}'",
        )
    if uses_scrub and scrub_marker is None:
        raise MalformedRedactionContractError(
            source=str(source),
            detail="a capture_scrubbed field is declared but no scrub_marker is",
        )

    return RedactionContract(
        scrub_marker=scrub_marker,
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


def _parse_content_policy(
    raw: Any, *, topic: str, source: Path
) -> ContentPolicy | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise MalformedRedactionContractError(
            source=str(source),
            detail=f"topic {topic!r} content_policy must be a mapping",
        )
    chunk = raw.get("chunk_chars")
    cap = raw.get("max_content_chars")
    if (
        not isinstance(chunk, int)
        or not isinstance(cap, int)
        or chunk < 1
        or cap < chunk
    ):
        raise MalformedRedactionContractError(
            source=str(source),
            detail=(
                f"topic {topic!r} content_policy needs integer chunk_chars >= 1 "
                "and max_content_chars >= chunk_chars"
            ),
        )
    names: dict[str, str] = {}
    for key in ("truncated_field", "original_field"):
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            raise MalformedRedactionContractError(
                source=str(source),
                detail=f"topic {topic!r} content_policy needs a non-empty {key}",
            )
        names[key] = value
    return ContentPolicy(
        chunk_chars=chunk,
        max_content_chars=cap,
        truncated_field=names["truncated_field"],
        original_field=names["original_field"],
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


def _string_leaves(value: Any) -> Iterator[str]:
    """Every string reachable inside a container, at any depth."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _string_leaves(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _string_leaves(item)


def _scrub_targets(value: Any) -> Iterator[str]:
    """The texts the secret scrub must see for one field's value.

    A string is matched as itself. Anything else is matched BOTH as its
    canonical JSON form -- so key/value framing a pattern needs is visible
    even when the framing is structural rather than textual -- and leaf by
    leaf, so a leaf whose JSON escaping would break a pattern is still seen
    raw. A hit anywhere redacts the whole field: a container is redacted as a
    unit because splitting it would publish the clean half of a value the
    scrub has already refused.
    """
    if isinstance(value, str):
        yield value
        return
    yield _canonical(value)
    yield from _string_leaves(value)


def _matches_secret(value: Any, contract: RedactionContract) -> str | None:
    for target in _scrub_targets(value):
        for name, pattern in contract.secret_patterns:
            if pattern.search(target):
                return name
    return None


def _scrub_str(text: str, contract: RedactionContract, hits: dict[str, int]) -> str:
    marker = contract.scrub_marker or ""
    for name, pattern in contract.secret_patterns:
        replacement = marker.format(name=name)
        # A literal replacement, never a template: the marker is contract text
        # and must not be read as a regex back-reference.
        text, count = pattern.subn(replacement.replace("\\", "\\\\"), text)
        if count:
            hits[name] = hits.get(name, 0) + count
    return text


def _scrub_value(value: Any, contract: RedactionContract, hits: dict[str, int]) -> Any:
    """Scrub every string leaf of ``value`` in place of its matched spans."""
    if isinstance(value, str):
        return _scrub_str(value, contract, hits)
    if isinstance(value, dict):
        return {k: _scrub_value(v, contract, hits) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub_value(v, contract, hits) for v in value]
    return value


def scrub_text(
    text: str, *, contract_path: Path | None = None
) -> tuple[str, dict[str, int]]:
    """Replace every secret-pattern span in ``text`` with the contract's marker.

    Returns the scrubbed text and ``{pattern name: count}``. Patterns apply in
    contract order. Idempotent by construction: the marker matches no pattern
    (asserted by test), so scrubbing scrubbed text changes nothing.
    """
    contract = load_contract(contract_path)
    if contract.scrub_marker is None:
        raise ValueError("the capture redaction contract declares no scrub_marker")
    hits: dict[str, int] = {}
    return _scrub_str(text, contract, hits), hits


def plan_content_chunks(
    text: str, topic: str, *, contract_path: Path | None = None
) -> ContentPlan:
    """Split one (already scrubbed) content item per ``topic``'s content_policy.

    Content beyond ``max_content_chars`` is not published: the plan says it was
    cut and how long it was. ``content_sha256`` covers the published chunks
    concatenated, so a reader can verify a reassembly. Empty content is one
    empty chunk, so an empty tool result is still a record rather than an
    absence.

    Raises:
        ValueError: the topic declares no content_policy.
    """
    contract = load_contract(contract_path)
    policy = contract.topics.get(topic)
    if policy is None or policy.content_policy is None:
        raise ValueError(f"topic {topic!r} declares no content_policy")
    bound = policy.content_policy
    kept = text[: bound.max_content_chars]
    step = bound.chunk_chars
    chunks = tuple(kept[i : i + step] for i in range(0, len(kept), step)) or ("",)
    return ContentPlan(
        chunks=chunks,
        original_chars=len(text),
        truncated=len(text) > bound.max_content_chars,
        content_sha256=hashlib.sha256(kept.encode("utf-8")).hexdigest(),
    )


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
    # OMN-17201: REDACTED is the FLOOR, not the outcome of having removed
    # something. ``EnumArtifactRedactionState.REDACTED`` is defined in
    # omnibase_core as "a redaction transform has been applied; the stored
    # bytes are sanitized", and that is true of every record leaving this
    # function: each field was resolved against a declared class, an
    # undeclared one was hashed by the fail-closed default, and the scrub ran
    # over everything that survived verbatim. ``raw`` means no posture was
    # applied, so it is not a reachable output here.
    #
    # This is load-bearing downstream, not cosmetic. The OMN-16979 egress gate
    # (omnibase_infra node_bus_forwarder_effect) refuses ``raw`` structurally
    # -- it is ArtifactStore's default, so admitting it would gate nothing --
    # while the same contract states that once this seam stamps, the two
    # governed hook topics cross. Both clauses hold only if this function
    # never stamps ``raw``. It did, and the live tool-executed payload is
    # entirely capture_verbatim, so 100% of that topic was refused at the
    # boundary (OMN-17201: 61 outbound lines, zero acknowledged).
    state = EnumRedactionState.REDACTED
    result: JsonDict = {}
    truncated_fields: set[str] = set()

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
            continue

        capture_class = policy.fields.get(field, contract.default_field_class)
        if forced is not None and field in contract.content_fields:
            capture_class = EnumCaptureClass.CAPTURE_HASHED

        if capture_class is EnumCaptureClass.NEVER_CAPTURE:
            continue
        if capture_class is EnumCaptureClass.CAPTURE_HASHED:
            result[field] = hash_value(value)
            continue
        if capture_class is EnumCaptureClass.CAPTURE_SHAPE_ONLY:
            result[field] = shape_of(value)
            continue

        if capture_class is EnumCaptureClass.CAPTURE_SCRUBBED:
            hits: dict[str, int] = {}
            scrubbed = _scrub_value(value, contract, hits)
            bound = policy.content_policy
            if (
                bound is not None
                and isinstance(scrubbed, str)
                and len(scrubbed) > bound.chunk_chars
            ):
                # A producer that did not chunk cannot put an oversize record
                # on the broker. The record says it was cut.
                scrubbed = scrubbed[: bound.chunk_chars]
                cut = True
            else:
                cut = False
            result[field] = scrubbed
            if cut and bound is not None:
                truncated_fields.add(bound.truncated_field)
            if hits:
                state = EnumRedactionState.SECRET_DETECTED
            continue

        # capture_verbatim -- still subject to the scrub.
        if _matches_secret(value, contract) is not None:
            result[field] = hash_value(value)
            state = EnumRedactionState.SECRET_DETECTED
        else:
            result[field] = value

    for name in truncated_fields:
        result[name] = True
    result[contract.redaction_state_field] = state.value
    return result


__all__: list[str] = [
    "ContentPlan",
    "ContentPolicy",
    "DerivedField",
    "EnumCaptureClass",
    "EnumRedactionState",
    "OutputClass",
    "RedactionContract",
    "TopicPolicy",
    "default_contract_path",
    "hash_value",
    "load_contract",
    "plan_content_chunks",
    "redact_capture",
    "scrub_text",
    "shape_of",
]
