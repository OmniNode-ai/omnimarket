# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Blocking drift validator for omniclaude hook topics and omnimarket registry.

The omnimarket emit daemon owns the portable YAML registry used at runtime.
omniclaude still owns the hook event registry and TopicBase enum. This validator
keeps those two contract surfaces synchronized and fails on new unbaselined
drift in either direction.
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

_DEFAULT_BASELINE = Path("scripts/validation/event_registry_drift_baseline.txt")
_DEFAULT_MARKET_REGISTRY = Path(
    "src/omnimarket/nodes/node_emit_daemon/registries/topics.yaml"
)
_OMNICLAUDE_EVENT_REGISTRY = Path("src/omniclaude/hooks/event_registry.py")
_OMNICLAUDE_TOPICS = Path("src/omniclaude/hooks/topics.py")


@dataclass(frozen=True)
class ModelEventRegistryDriftReport:
    """Symmetric drift report after applying the checked-in baseline."""

    source_only: frozenset[str]
    registry_only: frozenset[str]
    baselined_source_only: frozenset[str]
    baselined_registry_only: frozenset[str]

    @property
    def has_drift(self) -> bool:
        return bool(self.source_only or self.registry_only)


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


def load_omniclaude_event_topics(
    event_registry_path: Path, topics_path: Path
) -> set[str]:
    """Resolve TopicBase references used by omniclaude's hook event registry."""
    topic_values = load_topic_base_values(topics_path)
    tree = ast.parse(
        event_registry_path.read_text(encoding="utf-8"),
        filename=str(event_registry_path),
    )

    topics: set[str] = set()
    unresolved: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.keyword) or node.arg != "topic_base":
            continue
        value = node.value
        if (
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == "TopicBase"
        ):
            topic = topic_values.get(value.attr)
            if topic is None:
                unresolved.add(value.attr)
            else:
                topics.add(topic)

    if unresolved:
        unresolved_list = ", ".join(sorted(unresolved))
        raise ValueError(f"Unresolved TopicBase references: {unresolved_list}")
    if not topics:
        raise ValueError(
            f"No TopicBase fan-out references found in {event_registry_path}"
        )
    return topics


def load_market_registry_topics(registry_path: Path) -> set[str]:
    """Load all fan-out wire topics from the omnimarket emit-daemon YAML registry."""
    raw = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{registry_path} must contain a YAML mapping")
    events = raw.get("events")
    if not isinstance(events, dict):
        raise ValueError(f"{registry_path} must contain an events mapping")

    topics: set[str] = set()
    for event_type, event_def in events.items():
        if not isinstance(event_def, dict):
            raise ValueError(f"Event {event_type!r} must be a mapping")
        fan_out = event_def.get("fan_out", [])
        if not isinstance(fan_out, list):
            raise ValueError(f"Event {event_type!r} fan_out must be a list")
        for rule in fan_out:
            if not isinstance(rule, dict):
                raise ValueError(f"Event {event_type!r} fan_out rule must be a mapping")
            topic = rule.get("topic")
            if isinstance(topic, str) and topic.startswith("onex."):
                topics.add(topic)
    if not topics:
        raise ValueError(f"No fan-out topics found in {registry_path}")
    return topics


def load_baseline(path: Path) -> tuple[frozenset[str], frozenset[str]]:
    """Load baseline entries as `(source_only, registry_only)` topic sets."""
    if not path.exists():
        return frozenset(), frozenset()

    source_only: set[str] = set()
    registry_only: set[str] = set()
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ValueError(f"{path}:{line_number}: expected '<kind> <topic>'")
        kind, topic = parts
        if kind == "source_only":
            source_only.add(topic)
        elif kind == "registry_only":
            registry_only.add(topic)
        else:
            raise ValueError(
                f"{path}:{line_number}: kind must be source_only or registry_only"
            )
    return frozenset(source_only), frozenset(registry_only)


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


def validate_event_registry_drift(
    *,
    repo_root: Path,
    omniclaude_root: Path,
    market_registry_path: Path,
    baseline_path: Path,
) -> ModelEventRegistryDriftReport:
    """Validate that omniclaude event topics and omnimarket registry topics match."""
    source_topics = load_omniclaude_event_topics(
        omniclaude_root / _OMNICLAUDE_EVENT_REGISTRY,
        omniclaude_root / _OMNICLAUDE_TOPICS,
    )
    registry_topics = load_market_registry_topics(repo_root / market_registry_path)
    baseline_source_only, baseline_registry_only = load_baseline(
        repo_root / baseline_path
    )
    return compare_topic_sets(
        source_topics=source_topics,
        registry_topics=registry_topics,
        baseline_source_only=baseline_source_only,
        baseline_registry_only=baseline_registry_only,
    )


def _format_topic_lines(title: str, topics: frozenset[str]) -> list[str]:
    if not topics:
        return []
    return [title, *(f"  - {topic}" for topic in sorted(topics))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate omniclaude hook event topics against omnimarket registry"
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
            "Update src/omnimarket/nodes/node_emit_daemon/registries/topics.yaml "
            "or omniclaude hooks/TopicBase together, or add an explicit baseline "
            "entry for pre-existing debt.",
            *_format_topic_lines(
                "Source topics missing from omnimarket registry:",
                report.source_only,
            ),
            *_format_topic_lines(
                "Registry topics missing from omniclaude source:",
                report.registry_only,
            ),
        ]
        sys.stderr.write("\n".join(lines) + "\n")
        return 1

    sys.stdout.write(
        "OK: omniclaude hook event topics match omnimarket registry "
        f"({len(report.baselined_source_only)} source-only and "
        f"{len(report.baselined_registry_only)} registry-only baseline entries).\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
