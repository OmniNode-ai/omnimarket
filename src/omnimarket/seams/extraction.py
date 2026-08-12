# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Manifest-driven seam-graph extraction (OMN-15763).

Lives outside ``src/omnimarket/nodes/`` deliberately, mirroring
``omnibase_core.contract_graph`` (the library ``node_contract_graph_ir_compute``
delegates to for its own manifest-driven file discovery): the ONEX
node-purity gate scans node package directories for forbidden filesystem
calls, and the sanctioned pattern for a "pure" manifest-driven node is to
push the actual reads into a library module the node calls, not to avoid
reading files at all. Purity here rests on the methodology §2.2 argument —
the input is a pinned, already-materialized tree; checkout/fetch/ref
resolution happen upstream — not on the absence of ``Path.read_text()``.

Two extraction classes, kept separate:

* **Contract-declared** — every ``contract.yaml`` under the discovery roots
  is parsed for a ``seams:`` block (proposal step 1: declare seams where
  they are produced). This is the authoritative, typed source.
* **Code-level** — every ``*.py`` file under the discovery roots is parsed
  with ``ast`` for Kafka producer/consumer topic literals and ``os.environ``
  reads (comments and docstrings never produce a false observation, and
  multiline calls resolve correctly because ``ast`` — not a per-line regex —
  sees the whole call). ``@ref`` pins are scanned separately, restricted to
  actual comment tokens via ``tokenize`` (never a string literal that merely
  contains the substring ``@ref:``). These are raw evidence, not
  declarations.

**Symbol/constant resolution (OMN-15779).** A real call site almost never
passes a literal ``ast.Constant`` topic argument — it passes a ``Name``/
``Attribute`` assigned earlier (a module constant, a class attribute, a
``self.attr`` set in ``__init__``, or a dict-literal-backed registry
``.resolve(key)`` lookup). ``omnimarket.seams.symbol_resolution`` builds a
pure, deterministic, project-wide symbol table from the same parsed ASTs
this module already produces and resolves such a reference back to its
literal value when that is statically determinable. When it is genuinely
NOT statically determinable (an f-string, an unmodeled function call, an
``os.environ``-derived value), the call site still matched a real
producer/consumer receiver+method shape, so it is emitted as an explicit
``*_UNRESOLVED`` observation (``value`` = best-effort ``ast.unparse()`` of
the unresolved argument) — never silently dropped, never fabricated.
"""

from __future__ import annotations

import ast
import hashlib
import io
import re
import tokenize
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.seams.models.model_seam_graph import (
    EnumSeamGraphObservationKind,
    ModelSeamGraphCodeObservation,
    ModelSeamGraphEdgeDeclaration,
    ModelSeamGraphSourceHashEntry,
    ModelSeamGraphV1,
)
from omnimarket.seams.models.model_seam_projection import (
    EnumSeamDeliverySemantics,
    ModelSeamProjectionField,
)
from omnimarket.seams.symbol_resolution import (
    ProjectIndex,
    build_project_index,
    resolve_topic_expr,
)

__all__ = ["extract_seam_graph"]

_EXCLUDED_PATH_PARTS = frozenset({".venv", "__pycache__", "node_modules", ".git"})

_ENV_VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_REF_PIN_RE = re.compile(r"@ref:\s*(\S+)")
# ONEX topic names are versioned by convention:
# onex.<kind>.<domain>.<name>.vN — the trailing segment IS the wire
# envelope's version, already present in the topic string (OMN-15843).
_TOPIC_VERSION_SUFFIX_RE = re.compile(r"\.(v\d+)$")


def _repo_relative(path: Path, repo_base: Path) -> str:
    try:
        return str(path.relative_to(repo_base))
    except ValueError:
        return str(path)


def _is_excluded(path: Path, repo_base: Path) -> bool:
    """Exclude noise directories *within* the scanned tree only — never
    checks ancestor path segments above repo_base, since a pinned checkout
    may legitimately live under a directory a substring-match would
    otherwise wrongly exclude (e.g. a worktree root)."""

    relative_parts = Path(_repo_relative(path, repo_base)).parts
    return any(part in _EXCLUDED_PATH_PARTS for part in relative_parts)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resolve_confined_root(repo_base: Path, root: str) -> Path | None:
    """Resolve one discovery root against ``repo_base``, confined inside it.

    Returns ``None`` (never raises) for an invalid root so the caller can
    skip it and keep scanning the remaining valid roots:

    * an **absolute** ``root`` is rejected outright — pathlib silently
      discards the left operand when the right side of ``/`` is itself
      absolute (``Path("/base") / "/etc" == Path("/etc")``), so an absolute
      root would otherwise escape ``repo_base`` completely;
    * a ``..`` **traversal** or a **symlink** that resolves outside
      ``repo_base`` is rejected by requiring the resolved candidate to sit
      under the resolved base (``candidate.relative_to(resolved_base)``).
    """

    if Path(root).is_absolute():
        return None
    resolved_base = repo_base.resolve()
    candidate = (repo_base / root).resolve()
    try:
        candidate.relative_to(resolved_base)
    except ValueError:
        return None
    return candidate


def _discover_files(
    repo_base: Path, discovery_roots: tuple[str, ...], pattern: str
) -> list[Path]:
    found: set[Path] = set()
    for root in discovery_roots:
        root_path = _resolve_confined_root(repo_base, root)
        if root_path is None or not root_path.exists():
            continue
        for candidate in root_path.rglob(pattern):
            if candidate.is_file() and not _is_excluded(candidate, repo_base):
                found.add(candidate)
    return sorted(found)


def _parse_key_fields(raw: object) -> tuple[ModelSeamProjectionField, ...]:
    """Optional ``key_fields:`` list on a seams: entry — ``[{name, type}]``.

    Malformed entries (wrong type, missing key) are skipped individually
    rather than failing the whole edge, matching this reader's existing
    skip-not-fabricate posture for the entry itself."""

    if not isinstance(raw, list):
        return ()
    fields: list[ModelSeamProjectionField] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            fields.append(
                ModelSeamProjectionField(
                    name=item["name"], field_type=item["field_type"]
                )
            )
        except (KeyError, ValidationError):
            continue
    return tuple(fields)


def _parse_delivery_semantics(raw: object) -> EnumSeamDeliverySemantics:
    """Optional ``delivery_semantics:`` scalar — an unrecognized or absent
    value falls back to UNKNOWN, never a fabricated guess."""

    if isinstance(raw, str):
        try:
            return EnumSeamDeliverySemantics(raw)
        except ValueError:
            return EnumSeamDeliverySemantics.UNKNOWN
    return EnumSeamDeliverySemantics.UNKNOWN


def _parse_fsm_state_transitions(raw: object) -> tuple[str, ...]:
    """Optional ``fsm_state_transitions:`` list of state-name strings."""

    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, str) and item)


def _correlate_contract_paths(
    edges: list[ModelSeamGraphEdgeDeclaration],
) -> list[ModelSeamGraphEdgeDeclaration]:
    """Fill each edge's counterpart ``producer_contract_path`` /
    ``consumer_contract_path`` from any other declaration in this same scan
    that shares its ``edge_id`` with the opposite role — a producer-side
    declaration alone only knows its own path; the consumer path is only
    knowable once both sides have been discovered."""

    producer_path_by_edge: dict[str, str] = {}
    consumer_path_by_edge: dict[str, str] = {}
    for edge in edges:
        if edge.producer_contract_path is not None:
            producer_path_by_edge[edge.edge_id] = edge.producer_contract_path
        if edge.consumer_contract_path is not None:
            consumer_path_by_edge[edge.edge_id] = edge.consumer_contract_path

    correlated: list[ModelSeamGraphEdgeDeclaration] = []
    for edge in edges:
        producer_path = producer_path_by_edge.get(edge.edge_id)
        consumer_path = consumer_path_by_edge.get(edge.edge_id)
        if (
            producer_path == edge.producer_contract_path
            and consumer_path == edge.consumer_contract_path
        ):
            correlated.append(edge)
            continue
        correlated.append(
            edge.model_copy(
                update={
                    "producer_contract_path": producer_path,
                    "consumer_contract_path": consumer_path,
                }
            )
        )
    return correlated


def _load_contract_yaml(contract_path: Path) -> dict[str, object] | None:
    """Parse one ``contract.yaml`` once, shared by both declared-edge
    extractors below — avoids reading + parsing the same file twice per
    contract. Returns ``None`` for unreadable/malformed/non-mapping YAML so
    each caller can skip it uniformly."""

    try:
        raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def _extract_declared_edges(
    raw: dict[str, object], source_contract_path: str
) -> tuple[ModelSeamGraphEdgeDeclaration, ...]:
    """``seams:`` block extraction (proposal step 1) — a hand-authored,
    fully-typed declaration schema. Not yet adopted by any real contract in
    the traced repos; kept for forward-compat and because it is the only
    schema that can carry ``envelope_model``/``envelope_version``."""

    seams = raw.get("seams")
    if not isinstance(seams, list):
        return ()

    edges: list[ModelSeamGraphEdgeDeclaration] = []
    for entry in seams:
        if not isinstance(entry, dict):
            continue
        try:
            # No str(...) coercion: a wrong-typed field (e.g. a null or
            # numeric `seam`) must fail pydantic validation and be skipped,
            # not be silently stringified into a plausible-looking but wrong
            # value ("None", "123") that then poisons the emitted graph.
            role = entry["role"]
            edges.append(
                ModelSeamGraphEdgeDeclaration(
                    edge_id=entry["id"],
                    seam=entry["seam"],
                    role=role,
                    source_contract_path=source_contract_path,
                    topic=entry["topic"],
                    envelope_model=entry["envelope_model"],
                    envelope_version=entry["envelope_version"],
                    key_fields=_parse_key_fields(entry.get("key_fields")),
                    delivery_semantics=_parse_delivery_semantics(
                        entry.get("delivery_semantics")
                    ),
                    producer_contract_path=(
                        source_contract_path if role == "producer" else None
                    ),
                    consumer_contract_path=(
                        source_contract_path if role == "consumer" else None
                    ),
                    fsm_state_transitions=_parse_fsm_state_transitions(
                        entry.get("fsm_state_transitions")
                    ),
                )
            )
        except (KeyError, ValidationError):
            # A seams: entry missing a required key, or carrying a
            # wrong-typed value, is a malformed declaration, not a
            # code-level observation — skip it rather than fabricate or
            # coerce a partial edge. The contract-sweep-style gates own
            # flagging malformed contracts; this stays a pure reader.
            continue
    return tuple(edges)


def _extract_event_bus_edges(
    raw: dict[str, object], source_contract_path: str
) -> tuple[ModelSeamGraphEdgeDeclaration, ...]:
    """``event_bus.publish_topics`` / ``event_bus.subscribe_topics`` —
    the REAL declared-topic schema (OMN-15763 AC1 fix-forward). 435+
    contracts across omnibase_infra/omnimarket/omnibase_core carry this
    block; none carry a ``seams:`` block. Per repo CLAUDE.md convention,
    this — not a Kafka-client call-site literal — is the authoritative,
    contract-first source of a node's topic seams.

    ``edge_id`` is the topic string itself: ONEX topic names are globally
    unique dot-namespaced identifiers (``onex.evt.<domain>.<name>.v1``), so
    they are a real, non-fabricated correlation key — unlike the ``seams:``
    schema's hand-authored ``id:``, nothing here is invented. Multiple
    contracts publishing or subscribing the same topic (fan-out) each emit
    their own edge entry; ``_correlate_contract_paths`` then fills each
    entry's counterpart path from *some* declaring contract sharing that
    edge_id — a disclosed limitation for many-producer/many-consumer topics
    (the model carries a single counterpart path, not a list), not a
    fabrication of any individual entry's own fields.

    ``envelope_model``/``envelope_version`` are left ``None`` here — this
    schema does not declare either per-topic — and are filled in later by
    ``_apply_envelope_info``'s whole-graph correlation pass (OMN-15843):
    ``envelope_version`` from the topic's own ``.vN`` version suffix,
    ``envelope_model`` from any contract's ``published_events:`` block
    declaring that exact topic. Still ``None`` (not a fabricated
    placeholder) for any edge neither source can resolve.
    """

    event_bus = raw.get("event_bus")
    if not isinstance(event_bus, dict):
        return ()

    edges: list[ModelSeamGraphEdgeDeclaration] = []
    for role, topics_key in (
        ("producer", "publish_topics"),
        ("consumer", "subscribe_topics"),
    ):
        topics = event_bus.get(topics_key)
        if not isinstance(topics, list):
            continue
        for topic in topics:
            if not isinstance(topic, str) or not topic.strip():
                continue
            edges.append(
                ModelSeamGraphEdgeDeclaration(
                    edge_id=topic,
                    seam=topic,
                    role=role,
                    source_contract_path=source_contract_path,
                    topic=topic,
                    producer_contract_path=(
                        source_contract_path if role == "producer" else None
                    ),
                    consumer_contract_path=(
                        source_contract_path if role == "consumer" else None
                    ),
                )
            )
    return tuple(edges)


def _envelope_version_from_topic(topic: str) -> str | None:
    """Read the wire envelope version straight off the topic's own ``.vN``
    suffix (OMN-15843) — not a guess: ONEX topic names encode their version
    in the name itself (``onex.evt.<domain>.<name>.v1``). ``None`` for a
    topic that does not follow the convention, never a fabricated ``"v1"``
    default."""

    match = _TOPIC_VERSION_SUFFIX_RE.search(topic)
    if match is None:
        return None
    return match.group(1)


def _parse_published_events(raw: dict[str, object]) -> dict[str, str]:
    """``published_events:`` block (``ModelPublishedEventEntry`` shape:
    ``{topic, event_type}``) — a contract's own, already-declared per-topic
    wire-type name (OMN-15843). Present on a minority of contracts, but
    real, non-fabricated data where it exists. Malformed entries (wrong
    type, missing/blank field) are skipped individually, matching this
    module's existing skip-not-fabricate posture."""

    published = raw.get("published_events")
    if not isinstance(published, list):
        return {}
    result: dict[str, str] = {}
    for entry in published:
        if not isinstance(entry, dict):
            continue
        topic = entry.get("topic")
        event_type = entry.get("event_type")
        if (
            isinstance(topic, str)
            and topic.strip()
            and isinstance(event_type, str)
            and event_type.strip()
        ):
            result[topic] = event_type
    return result


def _apply_envelope_info(
    edges: list[ModelSeamGraphEdgeDeclaration],
    event_type_by_topic: dict[str, str],
) -> list[ModelSeamGraphEdgeDeclaration]:
    """Whole-graph correlation pass (OMN-15843), mirroring
    ``_correlate_contract_paths``'s shape: fills ``envelope_version`` from
    each edge's own topic version suffix, and ``envelope_model`` from
    ``event_type_by_topic`` (built from every scanned contract's
    ``published_events:`` block) keyed by the edge's topic — so a
    consumer-side edge inherits the same wire type its topic's producer
    already declared, since one topic has exactly one wire envelope.

    Only fills fields that are currently ``None`` — a genuinely-declared
    ``seams:`` block value (or an already-filled event_bus value) is never
    overwritten."""

    filled: list[ModelSeamGraphEdgeDeclaration] = []
    for edge in edges:
        update: dict[str, str] = {}
        if edge.envelope_model is None:
            resolved_model = event_type_by_topic.get(edge.topic)
            if resolved_model is not None:
                update["envelope_model"] = resolved_model
        if edge.envelope_version is None:
            resolved_version = _envelope_version_from_topic(edge.topic)
            if resolved_version is not None:
                update["envelope_version"] = resolved_version
        filled.append(edge.model_copy(update=update) if update else edge)
    return filled


def _string_constant(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _attr_base_name(value: ast.expr) -> str:
    """Best-effort trailing identifier for an attribute chain — e.g.
    ``self.producer`` and bare ``producer`` both yield ``"producer"`` — so
    the classifier matches both the fixture's bare-name idiom and a real
    call site's ``self.producer.send(...)`` shape."""

    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return ""


def _call_topic_literal(
    call: ast.Call, source_path: str, project_index: ProjectIndex
) -> str | None:
    """First string literal argument, unwrapping a single-element list
    (the ``consumer.subscribe(["topic"])`` idiom). Falls back to
    project-wide symbol resolution (OMN-15779) when the argument is not a
    direct literal — a module constant, class attribute, ``self.attr``
    chain, or registry ``.resolve(key)``/``.get(key)`` lookup."""

    if not call.args:
        return None
    first = call.args[0]
    literal = _string_constant(first)
    if literal is not None:
        return literal
    if isinstance(first, ast.List) and first.elts:
        list_literal = _string_constant(first.elts[0])
        if list_literal is not None:
            return list_literal
    return resolve_topic_expr(first, source_path, project_index)


_UNRESOLVED_KIND_BY_KIND: dict[
    EnumSeamGraphObservationKind, EnumSeamGraphObservationKind
] = {
    EnumSeamGraphObservationKind.PRODUCER_SEND: (
        EnumSeamGraphObservationKind.PRODUCER_SEND_UNRESOLVED
    ),
    EnumSeamGraphObservationKind.CONSUMER_SUBSCRIBE: (
        EnumSeamGraphObservationKind.CONSUMER_SUBSCRIBE_UNRESOLVED
    ),
}


def _best_effort_unparse(expr: ast.expr) -> str:
    """Diagnostic-only text for an unresolved argument expression -- never
    load-bearing correctness, so a pathological node that ``ast.unparse``
    cannot render simply yields no unresolved observation rather than
    raising."""

    try:
        return ast.unparse(expr)
    except (ValueError, TypeError, RecursionError):
        return ""


def _is_producer_receiver(name: str) -> bool:
    """Matches ``producer``, ``_producer``, ``kafka_producer``,
    ``event_producer``, etc. — any receiver whose trailing identifier
    component is or ends with ``producer`` — not only the bare exact name.
    Real call sites almost never use the bare name (``self._producer``,
    ``self.kafka_producer`` are the live idiom); requiring exact equality
    was the fixture-shaped false-negative the corpus run exposed."""

    lowered = name.lower()
    return lowered == "producer" or lowered.endswith("_producer")


def _is_consumer_receiver(name: str) -> bool:
    lowered = name.lower()
    return lowered == "consumer" or lowered.endswith("_consumer")


def _is_bus_receiver(name: str) -> bool:
    """Matches ``bus``, ``_bus``, ``event_bus``, ``typed_bus``, etc. — any
    receiver whose trailing identifier component is or ends with ``bus``.
    OMN-15779 corpus finding: the dominant real-corpus receiver for BOTH
    ``.publish()`` and ``.subscribe()`` is a bus reference
    (``self._event_bus``, ``self._bus``, a bare ``bus``/``typed_bus``
    parameter — see ``feedback_bus_is_the_transport``), essentially never a
    ``producer``/``consumer``-suffixed name. The previously-shipped
    ``_is_event_bus_receiver`` only matched the exact ``event_bus`` suffix,
    which matched zero real ``.subscribe()`` call sites in the traced
    corpus."""

    lowered = name.lower()
    return lowered == "bus" or lowered.endswith("_bus")


def _classify_call(call: ast.Call) -> EnumSeamGraphObservationKind | None:
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    base_name = _attr_base_name(func.value)
    if func.attr == "send" and _is_producer_receiver(base_name):
        return EnumSeamGraphObservationKind.PRODUCER_SEND
    if func.attr == "publish" and (
        _is_producer_receiver(base_name) or _is_bus_receiver(base_name)
    ):
        return EnumSeamGraphObservationKind.PRODUCER_SEND
    if func.attr == "subscribe" and (
        _is_consumer_receiver(base_name) or _is_bus_receiver(base_name)
    ):
        return EnumSeamGraphObservationKind.CONSUMER_SUBSCRIBE
    if (
        func.attr == "get"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "environ"
        and _attr_base_name(func.value.value) == "os"
    ):
        return EnumSeamGraphObservationKind.ENV_READ
    return None


def _is_os_environ_subscript(node: ast.Subscript) -> bool:
    value = node.value
    return (
        isinstance(value, ast.Attribute)
        and value.attr == "environ"
        and _attr_base_name(value.value) == "os"
    )


def _extract_ast_observations(
    tree: ast.AST, repo_relative: str, project_index: ProjectIndex
) -> list[ModelSeamGraphCodeObservation]:
    observations: list[ModelSeamGraphCodeObservation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            kind = _classify_call(node)
            if kind is None:
                continue
            if kind is EnumSeamGraphObservationKind.ENV_READ:
                value = _string_constant(node.args[0]) if node.args else None
                if not value or not value.strip():
                    continue
                if not _ENV_VAR_NAME_RE.match(value):
                    continue
                observations.append(
                    ModelSeamGraphCodeObservation(
                        source_path=repo_relative,
                        kind=kind,
                        value=value,
                        line_number=node.lineno,
                    )
                )
                continue
            value = _call_topic_literal(node, repo_relative, project_index)
            if value and value.strip():
                observations.append(
                    ModelSeamGraphCodeObservation(
                        source_path=repo_relative,
                        kind=kind,
                        value=value,
                        line_number=node.lineno,
                    )
                )
                continue
            # The call site genuinely matched a producer/consumer
            # receiver+method shape (a real seam) but the topic argument is
            # not statically resolvable -- disclose it explicitly rather
            # than dropping it silently (OMN-15779).
            unresolved_kind = _UNRESOLVED_KIND_BY_KIND.get(kind)
            if unresolved_kind is not None and node.args:
                unparsed = _best_effort_unparse(node.args[0])
                if unparsed:
                    observations.append(
                        ModelSeamGraphCodeObservation(
                            source_path=repo_relative,
                            kind=unresolved_kind,
                            value=unparsed,
                            line_number=node.lineno,
                        )
                    )
        elif isinstance(node, ast.Subscript) and _is_os_environ_subscript(node):
            value = _string_constant(node.slice)
            if value is not None and _ENV_VAR_NAME_RE.match(value):
                observations.append(
                    ModelSeamGraphCodeObservation(
                        source_path=repo_relative,
                        kind=EnumSeamGraphObservationKind.ENV_READ,
                        value=value,
                        line_number=node.lineno,
                    )
                )
    return observations


def _resolve_ref_pin_target(pin_value: str, repo_base: Path) -> str | None:
    """Resolve a ``<path>.yaml#<dotted.key>`` / ``<path>.yml#<dotted.key>``
    ``@ref:`` pin to the literal string value it names (OMN-15779 scope item
    3: "``@ref:`` indirection"). ``<path>`` is resolved confined inside
    ``repo_base`` (same safety rule as a discovery root — a pin is content
    read from the scanned source tree, not a trusted input). Returns
    ``None`` (never raises, never fabricates) for a missing file, malformed
    YAML, a missing key, or a non-string terminal value."""

    file_part, sep, key_path = pin_value.partition("#")
    if not sep or not key_path or not file_part.endswith((".yaml", ".yml")):
        return None
    target = _resolve_confined_root(repo_base, file_part)
    if target is None or not target.is_file():
        return None
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        return None
    current: object = raw
    for segment in key_path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return None
        current = current[segment]
    return current if isinstance(current, str) and current else None


def _extract_ref_pin_observations(
    text: str, repo_relative: str, repo_base: Path
) -> list[ModelSeamGraphCodeObservation]:
    """``@ref:`` pins, restricted to actual comment TOKENS (``tokenize``),
    never a string literal or docstring that merely contains the substring
    ``@ref:``. Each raw pin is additionally, best-effort, resolved to its
    target's underlying value (OMN-15779) -- the raw ``REF_PIN`` observation
    is always emitted regardless of whether that resolution succeeds."""

    observations: list[ModelSeamGraphCodeObservation] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type != tokenize.COMMENT:
                continue
            for match in _REF_PIN_RE.finditer(tok.string):
                pin_value = match.group(1)
                observations.append(
                    ModelSeamGraphCodeObservation(
                        source_path=repo_relative,
                        kind=EnumSeamGraphObservationKind.REF_PIN,
                        value=pin_value,
                        line_number=tok.start[0],
                    )
                )
                resolved = _resolve_ref_pin_target(pin_value, repo_base)
                if resolved is not None:
                    observations.append(
                        ModelSeamGraphCodeObservation(
                            source_path=repo_relative,
                            kind=EnumSeamGraphObservationKind.REF_PIN_RESOLVED,
                            value=resolved,
                            line_number=tok.start[0],
                        )
                    )
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return observations
    return observations


def _extract_yaml_ref_pin_observations(
    text: str, repo_relative: str, repo_base: Path
) -> list[ModelSeamGraphCodeObservation]:
    """``@ref:`` pins in YAML files — a real config ref is a STRING VALUE
    (``endpoint_ref: "@ref:configs/service_endpoints.yaml#backends.x"``),
    not a Python-style comment, so the ``tokenize``-COMMENT restriction used
    for ``.py`` files (which exists to reject a docstring/string-literal
    false positive) does not apply here: matching any line in a YAML file is
    correct, because a YAML value containing ``@ref:`` genuinely IS the pin
    the seam graph needs to observe. Line-based, not full YAML parsing, so a
    malformed YAML file still yields whatever pins are textually present."""

    observations: list[ModelSeamGraphCodeObservation] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for match in _REF_PIN_RE.finditer(line):
            # Strip a trailing YAML quote/bracket the greedy \S+ swallowed
            # (e.g. `"@ref:x.yaml#y"` — the pin's own value never legitimately
            # ends in one of these).
            value = match.group(1).rstrip("\"'`,)]}")
            if not value:
                continue
            observations.append(
                ModelSeamGraphCodeObservation(
                    source_path=repo_relative,
                    kind=EnumSeamGraphObservationKind.REF_PIN,
                    value=value,
                    line_number=line_number,
                )
            )
            resolved = _resolve_ref_pin_target(value, repo_base)
            if resolved is not None:
                observations.append(
                    ModelSeamGraphCodeObservation(
                        source_path=repo_relative,
                        kind=EnumSeamGraphObservationKind.REF_PIN_RESOLVED,
                        value=resolved,
                        line_number=line_number,
                    )
                )
    return observations


def _extract_code_observations(
    repo_relative: str,
    tree: ast.AST | None,
    text: str,
    repo_base: Path,
    project_index: ProjectIndex,
) -> tuple[ModelSeamGraphCodeObservation, ...]:
    observations: list[ModelSeamGraphCodeObservation] = []

    if tree is not None:
        observations.extend(
            _extract_ast_observations(tree, repo_relative, project_index)
        )

    observations.extend(_extract_ref_pin_observations(text, repo_relative, repo_base))

    return tuple(observations)


def extract_seam_graph(
    repo_base_path: str, discovery_roots: tuple[str, ...]
) -> ModelSeamGraphV1:
    """Walk ``discovery_roots`` (resolved against ``repo_base_path``, a
    pinned already-materialized tree) and emit ``seam-graph/v1`` plus the
    per-source sha256 manifest. Deterministic: sorted discovery, sorted
    output, so two runs over the same tree are byte-identical (AC7)."""

    repo_base = Path(repo_base_path).resolve()

    contract_paths = _discover_files(repo_base, discovery_roots, "contract.yaml")
    python_paths = _discover_files(repo_base, discovery_roots, "*.py")
    # "*.yaml"/"*.yml" is a strict superset of contract_paths (contract.yaml
    # matches "*.yaml" too) — scanned separately for @ref pins because a
    # real @ref pin lives in ANY yaml file's string values, not only
    # contract.yaml's, and the seams: block reader above only cares about
    # contract.yaml specifically.
    yaml_paths = _discover_files(
        repo_base, discovery_roots, "*.yaml"
    ) + _discover_files(repo_base, discovery_roots, "*.yml")

    edges: list[ModelSeamGraphEdgeDeclaration] = []
    event_type_by_topic: dict[str, str] = {}
    for contract_path in contract_paths:
        raw = _load_contract_yaml(contract_path)
        if raw is None:
            continue
        source_contract_path = _repo_relative(contract_path, repo_base)
        edges.extend(_extract_declared_edges(raw, source_contract_path))
        edges.extend(_extract_event_bus_edges(raw, source_contract_path))
        event_type_by_topic.update(_parse_published_events(raw))
    edges = _correlate_contract_paths(edges)
    edges = _apply_envelope_info(edges, event_type_by_topic)

    # Parse every python file exactly once, keyed by repo-relative path, so
    # both the project-wide symbol index (OMN-15779) and the per-file
    # observation pass share the same ASTs instead of re-parsing twice.
    parsed_python: dict[str, tuple[ast.AST | None, str]] = {}
    for python_path in python_paths:
        try:
            text = python_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree: ast.AST | None = ast.parse(text)
        except SyntaxError:
            tree = None
        parsed_python[_repo_relative(python_path, repo_base)] = (tree, text)

    project_index = build_project_index(parsed_python)

    code_observations: list[ModelSeamGraphCodeObservation] = []
    for repo_relative, (tree, text) in parsed_python.items():
        code_observations.extend(
            _extract_code_observations(
                repo_relative, tree, text, repo_base, project_index
            )
        )
    for yaml_path in yaml_paths:
        try:
            yaml_text = yaml_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        code_observations.extend(
            _extract_yaml_ref_pin_observations(
                yaml_text, _repo_relative(yaml_path, repo_base), repo_base
            )
        )

    manifest_source_paths = sorted(
        set(contract_paths) | set(python_paths) | set(yaml_paths)
    )
    manifest_entries: list[ModelSeamGraphSourceHashEntry] = []
    for source_path in manifest_source_paths:
        manifest_entries.append(
            ModelSeamGraphSourceHashEntry(
                source_path=_repo_relative(source_path, repo_base),
                source_sha256=_sha256_bytes(source_path.read_bytes()),
            )
        )

    return ModelSeamGraphV1(
        discovery_roots=discovery_roots,
        edges=tuple(sorted(edges, key=lambda e: e.edge_id)),
        code_observations=tuple(
            sorted(
                code_observations,
                key=lambda o: (o.source_path, o.line_number, o.kind.value, o.value),
            )
        ),
        source_manifest=tuple(
            sorted(manifest_entries, key=lambda entry: entry.source_path)
        ),
    )
