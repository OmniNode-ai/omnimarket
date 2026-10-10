# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19153: the emit daemon no longer fans out the retired DoD-verify spelling.

The omniclaude namespace's DoD-verify-completed topic had no consumer, a flat
telemetry payload incompatible with node_dod_verify's verdict, and a
``duty_critical`` tier here contradicted by a ``telemetry`` waiver in
omniclaude. The surviving spelling is the verify node's own terminal, which
node_projection_dod_verdict stores. The retired literal is assembled from
parts so this guard never matches itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_emit_daemon as emit_daemon_pkg
from omnimarket.validators.event_registry_drift import main as drift_main

RETIRED_TOPIC = "onex.evt.omniclaude." + "dod-verify-completed.v1"
RETIRED_EVENT_TYPE = "dod.verify" + ".completed"
_DAEMON_DIR = Path(emit_daemon_pkg.__file__).resolve().parent

pytestmark = pytest.mark.unit


def _load(relative: str) -> dict[str, object]:
    loaded = yaml.safe_load((_DAEMON_DIR / relative).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_the_registry_has_no_rule_for_the_retired_event_type() -> None:
    events = _load("registries/topics.yaml")["events"]
    assert isinstance(events, dict)
    assert RETIRED_EVENT_TYPE not in events
    # Positive control: the sibling DoD event type is still registered, so the
    # lookup above reads the real event table.
    assert "dod.guard.fired" in events


def test_no_fan_out_rule_targets_the_retired_topic() -> None:
    events = _load("registries/topics.yaml")["events"]
    assert isinstance(events, dict)
    targets = [
        rule["topic"]
        for event in events.values()
        for rule in (event or {}).get("fan_out", [])
    ]
    assert RETIRED_TOPIC not in targets
    assert "onex.evt.omniclaude.dod-guard-fired.v1" in targets


def test_the_daemon_contract_neither_publishes_nor_escapes_the_retired_topic() -> None:
    contract = _load("contract.yaml")
    publish = contract["event_bus"]["publish_topics"]  # type: ignore[index]
    external = contract["externally_consumed_topics"]
    assert RETIRED_TOPIC not in publish
    assert RETIRED_TOPIC not in external
    assert "onex.evt.omniclaude.dod-guard-fired.v1" in publish


@pytest.mark.parametrize("restore_retired_event", [False, True])
def test_drift_gate_rejects_restored_retired_event_with_checked_in_baseline(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    restore_retired_event: bool,
) -> None:
    """The real gate must catch a reintroduced duplicate, with a clean control."""
    surviving_topic = "onex.evt.omnimarket.dod-verify-completed.v1"
    hooks = tmp_path / "src" / "omniclaude" / "hooks"
    hooks.mkdir(parents=True)
    topics = (
        "from enum import StrEnum\n"
        "class TopicBase(StrEnum):\n"
        f"    SURVIVING = {surviving_topic!r}\n"
    )
    registrations = [
        "'verdict.completed': EventRegistration("
        "event_type='verdict.completed', "
        "fan_out=[FanOutRule(topic_base=TopicBase.SURVIVING)])"
    ]
    if restore_retired_event:
        topics += f"    RETIRED = {RETIRED_TOPIC!r}\n"
        registrations.append(
            f"{RETIRED_EVENT_TYPE!r}: EventRegistration("
            f"event_type={RETIRED_EVENT_TYPE!r}, "
            "fan_out=[FanOutRule(topic_base=TopicBase.RETIRED)])"
        )
    (hooks / "topics.py").write_text(topics, encoding="utf-8")
    (hooks / "event_registry.py").write_text(
        "EVENT_REGISTRY: dict = {" + ",".join(registrations) + "}\n",
        encoding="utf-8",
    )
    registry = tmp_path / "topics.yaml"
    registry.write_text(
        yaml.safe_dump(
            {"events": {"verdict.completed": {"fan_out": [{"topic": surviving_topic}]}}}
        ),
        encoding="utf-8",
    )

    repo_root = Path(__file__).resolve().parents[1]
    result = drift_main(
        [
            "--repo-root",
            str(repo_root),
            "--omniclaude-root",
            str(tmp_path),
            "--market-registry",
            str(registry),
        ]
    )
    output = capsys.readouterr()
    if restore_retired_event:
        assert result == 1, (
            "the checked-in baseline silently exempted the retired event"
        )
        assert RETIRED_TOPIC in output.err
        assert RETIRED_EVENT_TYPE in output.err
    else:
        assert result == 0
        assert "OK: omniclaude hook event registry matches" in output.out
