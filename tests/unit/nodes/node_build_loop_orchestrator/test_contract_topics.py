# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-derived topic wiring tests for node_build_loop_orchestrator."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_build_loop_orchestrator.handlers.handler_build_loop_orchestrator import (
    TOPIC_BUILD_LOOP_COMPLETED,
    TOPIC_BUILD_LOOP_FAILED,
    TOPIC_BUILD_LOOP_START,
    TOPIC_DOD_CHECKED,
    TOPIC_OVERSEER_VERIFICATION_COMPLETED,
    TOPIC_OVERSEER_VERIFY_REQUESTED,
    _load_topic_bindings,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CONTRACT = (
    _REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_build_loop_orchestrator"
    / "contract.yaml"
)
_HANDLER = (
    _REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_build_loop_orchestrator"
    / "handlers"
    / "handler_build_loop_orchestrator.py"
)


def _contract() -> dict[str, object]:
    raw = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _single(topics: list[str], fragment: str) -> str:
    matches = [topic for topic in topics if fragment in topic]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.unit
def test_topic_constants_match_contract_event_bus_topics() -> None:
    event_bus = _contract()["event_bus"]
    assert isinstance(event_bus, dict)
    subscribe_topics = event_bus["subscribe_topics"]
    publish_topics = event_bus["publish_topics"]
    assert isinstance(subscribe_topics, list)
    assert isinstance(publish_topics, list)

    assert (
        _single(subscribe_topics, "build-loop-orchestrator-start")
        == TOPIC_BUILD_LOOP_START
    )
    assert (
        _single(subscribe_topics, "overseer-verifier-completed")
        == TOPIC_OVERSEER_VERIFICATION_COMPLETED
    )
    assert (
        _single(publish_topics, "build-loop-orchestrator-completed")
        == TOPIC_BUILD_LOOP_COMPLETED
    )
    assert _single(publish_topics, "build-loop-failed") == TOPIC_BUILD_LOOP_FAILED
    assert _single(publish_topics, "build-loop-dod-checked") == TOPIC_DOD_CHECKED
    assert _single(publish_topics, "overseer-verify") == TOPIC_OVERSEER_VERIFY_REQUESTED


@pytest.mark.unit
def test_injected_contract_path_controls_runtime_topic_bindings(tmp_path: Path) -> None:
    raw = _contract()
    raw["event_bus"] = {
        "subscribe_topics": [
            "custom.cmd.build-loop-orchestrator-start.v2",
            "custom.evt.overseer-verifier-completed.v2",
        ],
        "publish_topics": [
            "custom.evt.build-loop-orchestrator-phase-transition.v2",
            "custom.evt.build-loop-orchestrator-completed.v2",
            "custom.evt.build-loop-failed.v2",
            "custom.evt.build-loop-dod-checked.v2",
            "custom.cmd.overseer-verify.v2",
        ],
    }
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    topics = _load_topic_bindings(contract_path)

    assert topics.start == "custom.cmd.build-loop-orchestrator-start.v2"
    assert topics.overseer_verification_completed == (
        "custom.evt.overseer-verifier-completed.v2"
    )
    assert topics.phase_transition == (
        "custom.evt.build-loop-orchestrator-phase-transition.v2"
    )
    assert topics.completed == "custom.evt.build-loop-orchestrator-completed.v2"
    assert topics.failed == "custom.evt.build-loop-failed.v2"
    assert topics.dod_checked == "custom.evt.build-loop-dod-checked.v2"
    assert topics.overseer_verify_requested == "custom.cmd.overseer-verify.v2"


@pytest.mark.unit
def test_handler_source_does_not_own_onex_topic_literals() -> None:
    source = _HANDLER.read_text(encoding="utf-8")

    assert "onex.cmd." not in source
    assert "onex.evt." not in source
