# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Blocking drift validator unifying the two event registries.

There are two surfaces that describe how omniclaude hook events fan out to
Kafka topics:

* The **emit-daemon-side registry** -- the portable YAML at
  ``src/omnimarket/nodes/node_emit_daemon/registries/topics.yaml``. This is the
  single canonical source of truth the emit daemon actually loads at runtime.
* The **hook-side registry** -- omniclaude's ``hooks/event_registry.py``
  (``EVENT_REGISTRY``) plus the ``TopicBase`` enum in ``hooks/topics.py``. This
  is a hand-maintained Python projection of the canonical YAML.

These two surfaces must agree on **every field**, not just the set of wire
topics. The earlier revision of this validator (OMN-10127) compared topic sets
only, so a divergence in ``required_fields`` or ``partition_key_field`` -- e.g.
``task.delegated`` was missing ``delegated_to`` on the YAML side -- passed
silently. OMN-13146 promotes the YAML to the single canonical registry and makes
this validator a full structural comparison: event-type membership plus, for
every shared event, the fan-out topic set, the ``partition_key_field``, and the
``required_fields``.

Legitimate, intentional divergence (emit-daemon-only events such as the
synthetic ``daemon.health.probe`` round-trip, or pre-existing debt) is declared
explicitly in the baseline file. Anything else fails the gate.
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_DEFAULT_BASELINE = Path("scripts/validation/event_registry_drift_baseline.txt")
_DEFAULT_MARKET_REGISTRY = Path(
    "src/omnimarket/nodes/node_emit_daemon/registries/topics.yaml"
)
_OMNICLAUDE_EVENT_REGISTRY = Path("src/omniclaude/hooks/event_registry.py")
_OMNICLAUDE_TOPICS = Path("src/omniclaude/hooks/topics.py")


# =============================================================================
# Structural registration model
# =============================================================================


@dataclass(frozen=True)
class EventRegistrationShape:
    """Comparable, transport-agnostic shape of a single event registration.

    Only the fields that must stay synchronized between the two registries are
    captured. Transforms are intentionally excluded: the hook side references
    Python callables while the YAML side references symbolic transform names, so
    they are not directly comparable and are validated independently by the
    emit-daemon registry loader.
    """

    event_type: str
    topics: frozenset[str]
    partition_key_field: str | None
    required_fields: tuple[str, ...]

    def field_diffs(
        self,
        other: EventRegistrationShape,
        *,
        baseline_source_only_topics: frozenset[str] = frozenset(),
        baseline_registry_only_topics: frozenset[str] = frozenset(),
    ) -> tuple[str, ...]:
        """Return human-readable diffs against the registry-side shape.

        ``self`` is the omniclaude (source) shape; ``other`` is the omnimarket
        (registry) shape. ``required_fields`` is compared order-insensitively
        because field ordering carries no wire semantics. Topic divergence that
        is already declared in the topic-level baseline is suppressed so the same
        intentional carve-out is not reported twice.
        """
        diffs: list[str] = []
        source_only_topics = (self.topics - other.topics) - baseline_source_only_topics
        registry_only_topics = (
            other.topics - self.topics
        ) - baseline_registry_only_topics
        if source_only_topics or registry_only_topics:
            diffs.append(
                f"topics: source={sorted(self.topics)} registry={sorted(other.topics)}"
            )
        if self.partition_key_field != other.partition_key_field:
            diffs.append(
                "partition_key_field: "
                f"source={self.partition_key_field!r} "
                f"registry={other.partition_key_field!r}"
            )
        if set(self.required_fields) != set(other.required_fields):
            diffs.append(
                f"required_fields: source={list(self.required_fields)} "
                f"registry={list(other.required_fields)}"
            )
        return tuple(diffs)


# =============================================================================
# Root resolution
# =============================================================================


def resolve_repo_root(start: Path | None = None) -> Path:
    """Resolve the omnimarket repository root."""
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").exists() and (
            candidate / "src" / "omnimarket"
        ).exists():
            return candidate
    raise FileNotFoundError("Could not resolve omnimarket repository root")


def resolve_omniclaude_root(
    *, repo_root: Path, explicit_root: Path | None = None
) -> Path:
    """Resolve omniclaude root from an explicit path, OMNI_HOME, or ancestors."""
    candidates: list[Path] = []
    if explicit_root is not None:
        candidates.append(explicit_root)
    omni_home = os.environ.get("OMNI_HOME")
    if omni_home:
        candidates.append(Path(omni_home) / "omniclaude")
    for ancestor in repo_root.parents:
        candidates.append(ancestor / "omniclaude")

    for candidate in candidates:
        if (candidate / _OMNICLAUDE_EVENT_REGISTRY).exists() and (
            candidate / _OMNICLAUDE_TOPICS
        ).exists():
            return candidate.resolve()

    raise FileNotFoundError(
        "Could not resolve omniclaude root with hooks/event_registry.py and "
        "hooks/topics.py"
    )


# =============================================================================
# omniclaude (hook-side) registry parsing
# =============================================================================


def load_topic_base_values(topics_path: Path) -> dict[str, str]:
    """Load TopicBase member names and wire topic values from topics.py."""
    tree = ast.parse(topics_path.read_text(encoding="utf-8"), filename=str(topics_path))
    topics: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "TopicBase":
            continue
        for statement in node.body:
            if (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
                and statement.value.value.startswith("onex.")
            ):
                topics[statement.targets[0].id] = statement.value.value
    if not topics:
        raise ValueError(f"No TopicBase topic values found in {topics_path}")
    return topics


def _find_event_registry_dict(tree: ast.Module) -> ast.Dict:
    """Locate the ``EVENT_REGISTRY`` literal dict node in the parsed module.

    The declaration is an annotated assignment
    (``EVENT_REGISTRY: dict[str, EventRegistration] = {...}``), so both
    ``AnnAssign`` and plain ``Assign`` are accepted defensively.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "EVENT_REGISTRY"
            and isinstance(node.value, ast.Dict)
        ):
            return node.value
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "EVENT_REGISTRY"
            and isinstance(node.value, ast.Dict)
        ):
            return node.value
    raise ValueError("No EVENT_REGISTRY literal dict found")


def _const_str(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _string_list(node: ast.expr) -> tuple[str, ...]:
    if not isinstance(node, ast.List):
        return ()
    out: list[str] = []
    for element in node.elts:
        value = _const_str(element)
        if value is not None:
            out.append(value)
    return tuple(out)


def load_omniclaude_registrations(
    event_registry_path: Path, topics_path: Path
) -> dict[str, EventRegistrationShape]:
    """Parse omniclaude's ``EVENT_REGISTRY`` into comparable shapes.

    The fan-out ``topic_base=TopicBase.<NAME>`` attribute references are
    resolved to their wire topic values via the ``TopicBase`` enum. Any
    unresolved reference is a hard error -- it means the source registry points
    at a topic the enum does not define.
    """
    topic_values = load_topic_base_values(topics_path)
    tree = ast.parse(
        event_registry_path.read_text(encoding="utf-8"),
        filename=str(event_registry_path),
    )
    registry_dict = _find_event_registry_dict(tree)

    shapes: dict[str, EventRegistrationShape] = {}
    unresolved: set[str] = set()

    for key_node, value_node in zip(
        registry_dict.keys, registry_dict.values, strict=True
    ):
        if key_node is None:
            raise ValueError("EVENT_REGISTRY contains a dict-unpacking entry")
        event_type = _const_str(key_node)
        if event_type is None:
            raise ValueError("EVENT_REGISTRY keys must be string literals")
        if not isinstance(value_node, ast.Call):
            raise ValueError(
                f"EVENT_REGISTRY[{event_type!r}] must be an EventRegistration(...) call"
            )

        topics: set[str] = set()
        partition_key_field: str | None = None
        required_fields: tuple[str, ...] = ()

        for keyword in value_node.keywords:
            if keyword.arg == "fan_out" and isinstance(keyword.value, ast.List):
                for rule in keyword.value.elts:
                    if not isinstance(rule, ast.Call):
                        continue
                    for rule_kw in rule.keywords:
                        if rule_kw.arg != "topic_base":
                            continue
                        attr = rule_kw.value
                        if (
                            isinstance(attr, ast.Attribute)
                            and isinstance(attr.value, ast.Name)
                            and attr.value.id == "TopicBase"
                        ):
                            wire = topic_values.get(attr.attr)
                            if wire is None:
                                unresolved.add(attr.attr)
                            else:
                                topics.add(wire)
            elif keyword.arg == "partition_key_field":
                partition_key_field = _const_str(keyword.value)
            elif keyword.arg == "required_fields":
                required_fields = _string_list(keyword.value)

        shapes[event_type] = EventRegistrationShape(
            event_type=event_type,
            topics=frozenset(topics),
            partition_key_field=partition_key_field,
            required_fields=required_fields,
        )

    if unresolved:
        unresolved_list = ", ".join(sorted(unresolved))
        raise ValueError(f"Unresolved TopicBase references: {unresolved_list}")
    if not shapes:
        raise ValueError(f"No event registrations parsed from {event_registry_path}")
    return shapes


# =============================================================================
# omnimarket (emit-daemon-side) registry parsing
# =============================================================================


def load_market_registrations(
    registry_path: Path,
) -> dict[str, EventRegistrationShape]:
    """Parse the omnimarket emit-daemon YAML registry into comparable shapes."""
    raw = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{registry_path} must contain a YAML mapping")
    events = raw.get("events")
    if not isinstance(events, dict):
        raise ValueError(f"{registry_path} must contain an events mapping")

    shapes: dict[str, EventRegistrationShape] = {}
    for event_type, event_def in events.items():
        if not isinstance(event_def, dict):
            raise ValueError(f"Event {event_type!r} must be a mapping")
        fan_out = event_def.get("fan_out", [])
        if not isinstance(fan_out, list):
            raise ValueError(f"Event {event_type!r} fan_out must be a list")

        topics: set[str] = set()
        for rule in fan_out:
            if not isinstance(rule, dict):
                raise ValueError(f"Event {event_type!r} fan_out rule must be a mapping")
            topic = rule.get("topic")
            if isinstance(topic, str) and topic.startswith("onex."):
                topics.add(topic)

        partition_key_field = event_def.get("partition_key_field")
        if partition_key_field is not None and not isinstance(partition_key_field, str):
            raise ValueError(
                f"Event {event_type!r} partition_key_field must be a string or null"
            )

        required_raw = event_def.get("required_fields", [])
        if not isinstance(required_raw, list):
            raise ValueError(f"Event {event_type!r} required_fields must be a list")
        required_fields = tuple(str(item) for item in required_raw)

        shapes[str(event_type)] = EventRegistrationShape(
            event_type=str(event_type),
            topics=frozenset(topics),
            partition_key_field=partition_key_field,
            required_fields=required_fields,
        )

    if not shapes:
        raise ValueError(f"No events found in {registry_path}")
    return shapes


# =============================================================================
# Baseline (declared, intentional divergence)
# =============================================================================


@dataclass(frozen=True)
class Baseline:
    """Declared, intentional divergence carved out of the drift gate.

    The legacy ``source_only`` / ``registry_only`` topic kinds are retained for
    backward compatibility with existing baseline entries and the topic-set
    report. ``event_source_only`` / ``event_registry_only`` whitelist an entire
    event type that legitimately exists on only one side.
    """

    source_only_topics: frozenset[str] = frozenset()
    registry_only_topics: frozenset[str] = frozenset()
    event_source_only: frozenset[str] = frozenset()
    event_registry_only: frozenset[str] = frozenset()


_EMPTY_BASELINE = Baseline()

_BASELINE_KINDS = {
    "source_only",
    "registry_only",
    "event_source_only",
    "event_registry_only",
}


def load_baseline(path: Path) -> Baseline:
    """Load the baseline file into a structured ``Baseline``."""
    if not path.exists():
        return Baseline()

    source_only_topics: set[str] = set()
    registry_only_topics: set[str] = set()
    event_source_only: set[str] = set()
    event_registry_only: set[str] = set()

    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ValueError(f"{path}:{line_number}: expected '<kind> <value>'")
        kind, value = parts
        if kind not in _BASELINE_KINDS:
            raise ValueError(
                f"{path}:{line_number}: kind must be one of "
                f"{', '.join(sorted(_BASELINE_KINDS))}"
            )
        if kind == "source_only":
            source_only_topics.add(value)
        elif kind == "registry_only":
            registry_only_topics.add(value)
        elif kind == "event_source_only":
            event_source_only.add(value)
        else:
            event_registry_only.add(value)

    return Baseline(
        source_only_topics=frozenset(source_only_topics),
        registry_only_topics=frozenset(registry_only_topics),
        event_source_only=frozenset(event_source_only),
        event_registry_only=frozenset(event_registry_only),
    )


# =============================================================================
# Topic-set comparison (retained — symmetric topic membership)
# =============================================================================


@dataclass(frozen=True)
class ModelEventRegistryDriftReport:
    """Symmetric topic-set drift report after applying the checked-in baseline."""

    source_only: frozenset[str]
    registry_only: frozenset[str]
    baselined_source_only: frozenset[str]
    baselined_registry_only: frozenset[str]

    @property
    def has_drift(self) -> bool:
        return bool(self.source_only or self.registry_only)


def compare_topic_sets(
    *,
    source_topics: set[str],
    registry_topics: set[str],
    baseline_source_only: frozenset[str] = frozenset(),
    baseline_registry_only: frozenset[str] = frozenset(),
) -> ModelEventRegistryDriftReport:
    """Compare source and registry topics, subtracting known baseline entries."""
    actual_source_only = source_topics - registry_topics
    actual_registry_only = registry_topics - source_topics
    return ModelEventRegistryDriftReport(
        source_only=frozenset(actual_source_only - baseline_source_only),
        registry_only=frozenset(actual_registry_only - baseline_registry_only),
        baselined_source_only=frozenset(actual_source_only & baseline_source_only),
        baselined_registry_only=frozenset(
            actual_registry_only & baseline_registry_only
        ),
    )


def topics_from_shapes(shapes: dict[str, EventRegistrationShape]) -> set[str]:
    """Flatten all fan-out wire topics across a set of registration shapes."""
    topics: set[str] = set()
    for shape in shapes.values():
        topics.update(shape.topics)
    return topics


# =============================================================================
# Structural comparison (event-type membership + per-event field diffs)
# =============================================================================


@dataclass(frozen=True)
class StructuralDriftReport:
    """Full structural drift report across both registries."""

    event_source_only: frozenset[str]
    event_registry_only: frozenset[str]
    field_diffs: dict[str, tuple[str, ...]] = field(default_factory=dict)
    baselined_event_source_only: frozenset[str] = frozenset()
    baselined_event_registry_only: frozenset[str] = frozenset()

    @property
    def has_drift(self) -> bool:
        return bool(
            self.event_source_only or self.event_registry_only or self.field_diffs
        )


def compare_registrations(
    *,
    source: dict[str, EventRegistrationShape],
    registry: dict[str, EventRegistrationShape],
    baseline: Baseline = _EMPTY_BASELINE,
) -> StructuralDriftReport:
    """Compare the two registries structurally, subtracting baseline carve-outs.

    Field-level diffs are reported only for events present on BOTH sides and
    not whitelisted as event-level baseline entries. An event whitelisted on one
    side is excluded from field comparison entirely.
    """
    source_types = set(source)
    registry_types = set(registry)

    raw_source_only = source_types - registry_types
    raw_registry_only = registry_types - source_types

    event_source_only = raw_source_only - baseline.event_source_only
    event_registry_only = raw_registry_only - baseline.event_registry_only

    field_diffs: dict[str, tuple[str, ...]] = {}
    for event_type in sorted(source_types & registry_types):
        if (
            event_type in baseline.event_source_only
            or event_type in baseline.event_registry_only
        ):
            continue
        diffs = source[event_type].field_diffs(
            registry[event_type],
            baseline_source_only_topics=baseline.source_only_topics,
            baseline_registry_only_topics=baseline.registry_only_topics,
        )
        if diffs:
            field_diffs[event_type] = diffs

    return StructuralDriftReport(
        event_source_only=frozenset(event_source_only),
        event_registry_only=frozenset(event_registry_only),
        field_diffs=field_diffs,
        baselined_event_source_only=frozenset(
            raw_source_only & baseline.event_source_only
        ),
        baselined_event_registry_only=frozenset(
            raw_registry_only & baseline.event_registry_only
        ),
    )


# =============================================================================
# Top-level validation entrypoint
# =============================================================================


@dataclass(frozen=True)
class CombinedDriftReport:
    """Aggregate of the topic-set and structural drift reports."""

    topic_report: ModelEventRegistryDriftReport
    structural_report: StructuralDriftReport

    @property
    def has_drift(self) -> bool:
        return self.topic_report.has_drift or self.structural_report.has_drift


def validate_event_registry_drift(
    *,
    repo_root: Path,
    omniclaude_root: Path,
    market_registry_path: Path,
    baseline_path: Path,
) -> CombinedDriftReport:
    """Validate that the omniclaude hook registry matches the omnimarket YAML."""
    source = load_omniclaude_registrations(
        omniclaude_root / _OMNICLAUDE_EVENT_REGISTRY,
        omniclaude_root / _OMNICLAUDE_TOPICS,
    )
    registry = load_market_registrations(repo_root / market_registry_path)
    baseline = load_baseline(repo_root / baseline_path)

    topic_report = compare_topic_sets(
        source_topics=topics_from_shapes(source),
        registry_topics=topics_from_shapes(registry),
        baseline_source_only=baseline.source_only_topics,
        baseline_registry_only=baseline.registry_only_topics,
    )
    structural_report = compare_registrations(
        source=source,
        registry=registry,
        baseline=baseline,
    )
    return CombinedDriftReport(
        topic_report=topic_report,
        structural_report=structural_report,
    )


def _format_topic_lines(title: str, topics: frozenset[str]) -> list[str]:
    if not topics:
        return []
    return [title, *(f"  - {topic}" for topic in sorted(topics))]


def _format_field_diff_lines(
    title: str, field_diffs: dict[str, tuple[str, ...]]
) -> list[str]:
    if not field_diffs:
        return []
    lines = [title]
    for event_type in sorted(field_diffs):
        lines.append(f"  - {event_type}:")
        lines.extend(f"      {diff}" for diff in field_diffs[event_type])
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the omniclaude hook event registry against the canonical "
            "omnimarket emit-daemon registry (structural, field-level)."
        )
    )
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--omniclaude-root", type=Path, default=None)
    parser.add_argument(
        "--market-registry",
        type=Path,
        default=_DEFAULT_MARKET_REGISTRY,
    )
    parser.add_argument("--baseline", type=Path, default=_DEFAULT_BASELINE)
    args = parser.parse_args(argv)

    repo_root = resolve_repo_root(args.repo_root)
    omniclaude_root = resolve_omniclaude_root(
        repo_root=repo_root,
        explicit_root=args.omniclaude_root,
    )
    report = validate_event_registry_drift(
        repo_root=repo_root,
        omniclaude_root=omniclaude_root,
        market_registry_path=args.market_registry,
        baseline_path=args.baseline,
    )

    if report.has_drift:
        lines = [
            "ERROR: event registry drift detected.",
            "The canonical registry is "
            "src/omnimarket/nodes/node_emit_daemon/registries/topics.yaml. Update "
            "it and the omniclaude hooks/event_registry.py + TopicBase together, "
            "or add an explicit baseline entry for intentional divergence.",
            *_format_topic_lines(
                "Source topics missing from omnimarket registry:",
                report.topic_report.source_only,
            ),
            *_format_topic_lines(
                "Registry topics missing from omniclaude source:",
                report.topic_report.registry_only,
            ),
            *_format_topic_lines(
                "Event types in omniclaude source but not in registry:",
                report.structural_report.event_source_only,
            ),
            *_format_topic_lines(
                "Event types in registry but not in omniclaude source:",
                report.structural_report.event_registry_only,
            ),
            *_format_field_diff_lines(
                "Per-event field divergence (source vs registry):",
                report.structural_report.field_diffs,
            ),
        ]
        sys.stderr.write("\n".join(lines) + "\n")
        return 1

    structural = report.structural_report
    topic = report.topic_report
    sys.stdout.write(
        "OK: omniclaude hook event registry matches the canonical omnimarket "
        "registry "
        f"({len(topic.baselined_source_only)} source-only and "
        f"{len(topic.baselined_registry_only)} registry-only baselined topics; "
        f"{len(structural.baselined_event_source_only)} source-only and "
        f"{len(structural.baselined_event_registry_only)} registry-only "
        "baselined event types).\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
