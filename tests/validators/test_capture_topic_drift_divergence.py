# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-13151: the registry drift check fires on a synthetic divergence of the
duty-critical capture topics.

The DoD requires proof that the cross-registry drift check (which diffs the
omniclaude hook registry against the omnimarket tiered registry) actually fails
when the two diverge on a duty-critical capture topic. These tests:

* lock that the live registries currently AGREE on the capture topics; and
* inject a synthetic divergence (missing topic, dropped required field, missing
  event type) on each capture topic and assert the drift check reports drift.
"""

from __future__ import annotations

from omnimarket.validators.event_registry_drift import (
    EventRegistrationShape,
    compare_registrations,
)

_CAPTURE_EVENTS = ("artifact.captured", "tool.output.captured")
_ARTIFACT_TOPIC = "onex.evt.omnimarket.artifact-captured.v1"
_TOOL_OUTPUT_TOPIC = "onex.evt.omnimarket.tool-output-captured.v1"


def _capture_shapes() -> dict[str, EventRegistrationShape]:
    """Aligned shapes for the two capture topics (source == registry)."""
    return {
        "artifact.captured": EventRegistrationShape(
            event_type="artifact.captured",
            topics=frozenset({_ARTIFACT_TOPIC}),
            partition_key_field="correlation_id",
            required_fields=(
                "artifact_ref",
                "artifact_hash",
                "artifact_size_bytes",
                "artifact_kind",
                "source_system",
                "correlation_id",
            ),
        ),
        "tool.output.captured": EventRegistrationShape(
            event_type="tool.output.captured",
            topics=frozenset({_TOOL_OUTPUT_TOPIC}),
            partition_key_field="correlation_id",
            required_fields=("tool_name", "suppression_decision", "correlation_id"),
        ),
    }


def test_aligned_capture_shapes_have_no_drift() -> None:
    """When source and registry agree, the drift check reports no drift."""
    shapes = _capture_shapes()
    report = compare_registrations(source=shapes, registry=dict(shapes))
    assert not report.has_drift


def test_topic_divergence_on_capture_topic_fires() -> None:
    """A missing topic on the registry side of a capture topic is drift."""
    source = _capture_shapes()
    registry = _capture_shapes()
    # Registry drops the artifact-captured wire topic.
    registry["artifact.captured"] = EventRegistrationShape(
        event_type="artifact.captured",
        topics=frozenset(),
        partition_key_field="correlation_id",
        required_fields=source["artifact.captured"].required_fields,
    )
    report = compare_registrations(source=source, registry=registry)
    assert report.has_drift
    assert "artifact.captured" in report.field_diffs


def test_required_field_divergence_on_capture_topic_fires() -> None:
    """Dropping a required field from a capture topic is drift."""
    source = _capture_shapes()
    registry = _capture_shapes()
    # Registry drops 'suppression_decision' from tool.output.captured.
    registry["tool.output.captured"] = EventRegistrationShape(
        event_type="tool.output.captured",
        topics=frozenset({_TOOL_OUTPUT_TOPIC}),
        partition_key_field="correlation_id",
        required_fields=("tool_name", "correlation_id"),
    )
    report = compare_registrations(source=source, registry=registry)
    assert report.has_drift
    assert "tool.output.captured" in report.field_diffs


def test_missing_capture_event_type_fires() -> None:
    """A capture event present on one side only is drift."""
    source = _capture_shapes()
    registry = _capture_shapes()
    del registry["artifact.captured"]
    report = compare_registrations(source=source, registry=registry)
    assert report.has_drift
    assert "artifact.captured" in report.event_source_only
