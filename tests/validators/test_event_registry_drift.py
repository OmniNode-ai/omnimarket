# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the unified event-registry drift validator (OMN-13146).

Coverage:
* topic-set membership drift (retained from OMN-10127)
* event-type membership drift (new, structural)
* per-event field drift -- required_fields, partition_key_field, fan-out topics
* baseline suppression at both the topic and event-type level
* the live canonical registries agree (regression lock)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.validators.event_registry_drift import (
    Baseline,
    EventRegistrationShape,
    compare_registrations,
    compare_topic_sets,
    load_baseline,
    load_market_registrations,
    load_omniclaude_registrations,
    resolve_omniclaude_root,
    resolve_repo_root,
    topics_from_shapes,
    validate_event_registry_drift,
)

_DEFAULT_BASELINE = Path("scripts/validation/event_registry_drift_baseline.txt")
_DEFAULT_MARKET_REGISTRY = Path(
    "src/omnimarket/nodes/node_emit_daemon/registries/topics.yaml"
)


def _write_minimal_pair(
    tmp_path: Path,
    *,
    omniclaude_registry: str,
    omniclaude_topics: str,
    market_yaml: str,
) -> tuple[Path, Path, Path]:
    topics_path = tmp_path / "topics.py"
    event_registry_path = tmp_path / "event_registry.py"
    registry_path = tmp_path / "topics.yaml"
    topics_path.write_text(omniclaude_topics)
    event_registry_path.write_text(omniclaude_registry)
    registry_path.write_text(market_yaml)
    return event_registry_path, topics_path, registry_path


# =============================================================================
# Topic-set membership (retained behavior)
# =============================================================================


@pytest.mark.unit
def test_topic_in_source_only_is_drift(tmp_path: Path) -> None:
    event_registry_path, topics_path, registry_path = _write_minimal_pair(
        tmp_path,
        omniclaude_topics=(
            "from enum import StrEnum\n"
            "class TopicBase(StrEnum):\n"
            '    SESSION_STARTED = "onex.evt.omniclaude.session-started.v1"\n'
            '    MISSING = "onex.evt.omniclaude.missing.v1"\n'
        ),
        omniclaude_registry=(
            "EVENT_REGISTRY: dict = {\n"
            "    'session.started': EventRegistration(\n"
            "        event_type='session.started',\n"
            "        fan_out=[FanOutRule(topic_base=TopicBase.SESSION_STARTED)],\n"
            "    ),\n"
            "    'missing': EventRegistration(\n"
            "        event_type='missing',\n"
            "        fan_out=[FanOutRule(topic_base=TopicBase.MISSING)],\n"
            "    ),\n"
            "}\n"
        ),
        market_yaml=(
            "---\nevents:\n"
            "  session.started:\n"
            "    fan_out:\n"
            '      - topic: "onex.evt.omniclaude.session-started.v1"\n'
        ),
    )

    source = load_omniclaude_registrations(event_registry_path, topics_path)
    registry = load_market_registrations(registry_path)
    report = compare_topic_sets(
        source_topics=topics_from_shapes(source),
        registry_topics=topics_from_shapes(registry),
    )

    assert report.source_only == frozenset({"onex.evt.omniclaude.missing.v1"})
    assert report.registry_only == frozenset()
    assert report.has_drift is True


@pytest.mark.unit
def test_topic_in_registry_only_is_drift(tmp_path: Path) -> None:
    event_registry_path, topics_path, registry_path = _write_minimal_pair(
        tmp_path,
        omniclaude_topics=(
            "from enum import StrEnum\n"
            "class TopicBase(StrEnum):\n"
            '    SESSION_STARTED = "onex.evt.omniclaude.session-started.v1"\n'
        ),
        omniclaude_registry=(
            "EVENT_REGISTRY: dict = {\n"
            "    'session.started': EventRegistration(\n"
            "        event_type='session.started',\n"
            "        fan_out=[FanOutRule(topic_base=TopicBase.SESSION_STARTED)],\n"
            "    ),\n"
            "}\n"
        ),
        market_yaml=(
            "---\nevents:\n"
            "  session.started:\n"
            "    fan_out:\n"
            '      - topic: "onex.evt.omniclaude.session-started.v1"\n'
            "  extra.event:\n"
            "    fan_out:\n"
            '      - topic: "onex.evt.omniclaude.extra.v1"\n'
        ),
    )

    source = load_omniclaude_registrations(event_registry_path, topics_path)
    registry = load_market_registrations(registry_path)
    report = compare_topic_sets(
        source_topics=topics_from_shapes(source),
        registry_topics=topics_from_shapes(registry),
    )

    assert report.source_only == frozenset()
    assert report.registry_only == frozenset({"onex.evt.omniclaude.extra.v1"})
    assert report.has_drift is True


# =============================================================================
# Structural: event-type membership
# =============================================================================


@pytest.mark.unit
def test_event_registry_only_is_structural_drift() -> None:
    source = {
        "a": EventRegistrationShape(
            "a", frozenset({"onex.evt.x.a.v1"}), "sid", ("sid",)
        )
    }
    registry = {
        "a": EventRegistrationShape(
            "a", frozenset({"onex.evt.x.a.v1"}), "sid", ("sid",)
        ),
        "b": EventRegistrationShape(
            "b", frozenset({"onex.evt.x.b.v1"}), "sid", ("sid",)
        ),
    }
    report = compare_registrations(source=source, registry=registry)
    assert report.event_registry_only == frozenset({"b"})
    assert report.event_source_only == frozenset()
    assert report.field_diffs == {}
    assert report.has_drift is True


@pytest.mark.unit
def test_event_source_only_is_structural_drift() -> None:
    source = {
        "a": EventRegistrationShape(
            "a", frozenset({"onex.evt.x.a.v1"}), "sid", ("sid",)
        ),
        "z": EventRegistrationShape(
            "z", frozenset({"onex.evt.x.z.v1"}), "sid", ("sid",)
        ),
    }
    registry = {
        "a": EventRegistrationShape(
            "a", frozenset({"onex.evt.x.a.v1"}), "sid", ("sid",)
        )
    }
    report = compare_registrations(source=source, registry=registry)
    assert report.event_source_only == frozenset({"z"})
    assert report.has_drift is True


# =============================================================================
# Structural: per-event field drift
# =============================================================================


@pytest.mark.unit
def test_required_fields_drift_is_caught() -> None:
    """The exact OMN-13146 regression: required_fields skew on a shared event."""
    source = {
        "task.delegated": EventRegistrationShape(
            "task.delegated",
            frozenset({"onex.evt.omniclaude.task-delegated.v1"}),
            "session_id",
            ("session_id", "correlation_id", "task_type", "delegated_to"),
        )
    }
    registry = {
        "task.delegated": EventRegistrationShape(
            "task.delegated",
            frozenset({"onex.evt.omniclaude.task-delegated.v1"}),
            "session_id",
            ("session_id", "correlation_id", "task_type"),
        )
    }
    report = compare_registrations(source=source, registry=registry)
    assert "task.delegated" in report.field_diffs
    assert any("required_fields" in d for d in report.field_diffs["task.delegated"])
    assert report.has_drift is True


@pytest.mark.unit
def test_required_fields_order_is_not_drift() -> None:
    source = {
        "e": EventRegistrationShape(
            "e", frozenset({"onex.evt.x.e.v1"}), "sid", ("a", "b", "c")
        )
    }
    registry = {
        "e": EventRegistrationShape(
            "e", frozenset({"onex.evt.x.e.v1"}), "sid", ("c", "a", "b")
        )
    }
    report = compare_registrations(source=source, registry=registry)
    assert report.field_diffs == {}
    assert report.has_drift is False


@pytest.mark.unit
def test_partition_key_drift_is_caught() -> None:
    source = {
        "e": EventRegistrationShape(
            "e", frozenset({"onex.evt.x.e.v1"}), "session_id", ("session_id",)
        )
    }
    registry = {
        "e": EventRegistrationShape(
            "e", frozenset({"onex.evt.x.e.v1"}), "run_id", ("session_id",)
        )
    }
    report = compare_registrations(source=source, registry=registry)
    assert "e" in report.field_diffs
    assert any("partition_key_field" in d for d in report.field_diffs["e"])


@pytest.mark.unit
def test_per_event_topic_set_drift_is_caught() -> None:
    source = {
        "e": EventRegistrationShape(
            "e",
            frozenset({"onex.evt.x.e.v1", "onex.cmd.x.e.v1"}),
            "sid",
            ("sid",),
        )
    }
    registry = {
        "e": EventRegistrationShape(
            "e", frozenset({"onex.evt.x.e.v1"}), "sid", ("sid",)
        )
    }
    report = compare_registrations(source=source, registry=registry)
    assert "e" in report.field_diffs
    assert any("topics" in d for d in report.field_diffs["e"])


# =============================================================================
# Baseline suppression
# =============================================================================


@pytest.mark.unit
def test_topic_baseline_suppresses_known_but_not_new() -> None:
    baseline = Baseline(
        source_only_topics=frozenset({"onex.evt.x.known-source.v1"}),
        registry_only_topics=frozenset({"onex.evt.x.known-registry.v1"}),
    )
    report = compare_topic_sets(
        source_topics={
            "onex.evt.x.shared.v1",
            "onex.evt.x.known-source.v1",
            "onex.evt.x.new-source.v1",
        },
        registry_topics={
            "onex.evt.x.shared.v1",
            "onex.evt.x.known-registry.v1",
            "onex.evt.x.new-registry.v1",
        },
        baseline_source_only=baseline.source_only_topics,
        baseline_registry_only=baseline.registry_only_topics,
    )
    assert report.baselined_source_only == frozenset({"onex.evt.x.known-source.v1"})
    assert report.baselined_registry_only == frozenset({"onex.evt.x.known-registry.v1"})
    assert report.source_only == frozenset({"onex.evt.x.new-source.v1"})
    assert report.registry_only == frozenset({"onex.evt.x.new-registry.v1"})


@pytest.mark.unit
def test_event_baseline_suppresses_registry_only_event() -> None:
    source = {
        "a": EventRegistrationShape(
            "a", frozenset({"onex.evt.x.a.v1"}), "sid", ("sid",)
        )
    }
    registry = {
        "a": EventRegistrationShape(
            "a", frozenset({"onex.evt.x.a.v1"}), "sid", ("sid",)
        ),
        "daemon.health.probe": EventRegistrationShape(
            "daemon.health.probe",
            frozenset({"onex.evt.diagnostic.daemon-health.v1"}),
            "correlation_id",
            ("correlation_id", "probe"),
        ),
    }
    baseline = Baseline(event_registry_only=frozenset({"daemon.health.probe"}))
    report = compare_registrations(source=source, registry=registry, baseline=baseline)
    assert report.event_registry_only == frozenset()
    assert report.baselined_event_registry_only == frozenset({"daemon.health.probe"})
    assert report.has_drift is False


@pytest.mark.unit
def test_baselined_topic_does_not_retrigger_as_field_diff() -> None:
    """A topic already baselined at the topic level must not re-fire per-event."""
    source = {
        "diagnostic.daemon.health": EventRegistrationShape(
            "diagnostic.daemon.health",
            frozenset({"onex.evt.omniclaude.diagnostic-daemon-health.v1"}),
            "daemon_id",
            ("daemon_id",),
        )
    }
    registry = {
        "diagnostic.daemon.health": EventRegistrationShape(
            "diagnostic.daemon.health",
            frozenset(
                {
                    "onex.evt.omniclaude.diagnostic-daemon-health.v1",
                    "onex.evt.diagnostic.daemon-health.v1",
                }
            ),
            "daemon_id",
            ("daemon_id",),
        )
    }
    baseline = Baseline(
        registry_only_topics=frozenset({"onex.evt.diagnostic.daemon-health.v1"})
    )
    report = compare_registrations(source=source, registry=registry, baseline=baseline)
    assert report.field_diffs == {}
    assert report.has_drift is False


@pytest.mark.unit
def test_baseline_file_round_trip(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline.txt"
    baseline_path.write_text(
        "# comment\n"
        "source_only onex.evt.x.s.v1\n"
        "registry_only onex.evt.x.r.v1\n"
        "event_source_only some.source.event\n"
        "event_registry_only daemon.health.probe\n"
    )
    baseline = load_baseline(baseline_path)
    assert baseline.source_only_topics == frozenset({"onex.evt.x.s.v1"})
    assert baseline.registry_only_topics == frozenset({"onex.evt.x.r.v1"})
    assert baseline.event_source_only == frozenset({"some.source.event"})
    assert baseline.event_registry_only == frozenset({"daemon.health.probe"})


@pytest.mark.unit
def test_baseline_rejects_unknown_kind(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline.txt"
    baseline_path.write_text("bogus onex.evt.x.s.v1\n")
    with pytest.raises(ValueError, match="kind must be one of"):
        load_baseline(baseline_path)


# =============================================================================
# Live canonical registries agree (regression lock)
# =============================================================================


@pytest.mark.unit
def test_live_registries_have_no_unbaselined_drift() -> None:
    """The shipped omnimarket YAML and omniclaude hook registry must agree.

    Skips only when the omniclaude clone is not resolvable (e.g. an isolated
    CI shard without the sibling checkout); the dedicated Event Registry Drift
    CI job clones omniclaude explicitly and always runs this assertion.
    """
    repo_root = resolve_repo_root()
    try:
        omniclaude_root = resolve_omniclaude_root(repo_root=repo_root)
    except FileNotFoundError:
        pytest.skip("omniclaude clone not resolvable in this environment")

    report = validate_event_registry_drift(
        repo_root=repo_root,
        omniclaude_root=omniclaude_root,
        market_registry_path=_DEFAULT_MARKET_REGISTRY,
        baseline_path=_DEFAULT_BASELINE,
    )
    assert report.has_drift is False, (
        "Live event registries drifted: "
        f"topic_source_only={sorted(report.topic_report.source_only)}, "
        f"topic_registry_only={sorted(report.topic_report.registry_only)}, "
        f"event_source_only={sorted(report.structural_report.event_source_only)}, "
        f"event_registry_only={sorted(report.structural_report.event_registry_only)}, "
        f"field_diffs={report.structural_report.field_diffs}"
    )
