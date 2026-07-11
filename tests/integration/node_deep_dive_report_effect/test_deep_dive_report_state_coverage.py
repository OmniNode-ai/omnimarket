# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared-state coverage for node_deep_dive_report_effect."""

from __future__ import annotations

from pathlib import Path

import yaml


def _load_contract() -> dict[str, object]:
    path = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_deep_dive_report_effect"
        / "contract.yaml"
    )
    return yaml.safe_load(path.read_text())


def test_deep_dive_report_failed_topic_is_declared() -> None:
    contract = _load_contract()
    event_bus = contract["event_bus"]
    assert isinstance(event_bus, dict)
    publish_topics = event_bus["publish_topics"]
    assert "onex.evt.omnimarket.deep-dive-report-failed.v1" in publish_topics
