# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host disk orchestrator's contract declares the topics it publishes and the ones it reads from outside."""

from __future__ import annotations

import yaml

from .orchestrator_inputs import NODE_DIR


def _contract() -> dict[str, object]:
    loaded = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_published_topics_are_the_declared_four() -> None:
    event_bus = _contract()["event_bus"]
    assert isinstance(event_bus, dict)
    assert sorted(event_bus["publish_topics"]) == [
        "onex.cmd.omnimarket.host-disk-evict-requested.v1",
        "onex.cmd.omnimarket.slack-publish.v1",
        "onex.evt.omnimarket.host-disk-run-completed.v1",
        "onex.evt.omnimarket.schedule-run.v1",
    ]


def test_result_topics_it_reads_are_declared_with_a_named_producer() -> None:
    produced = _contract()["externally_produced_topics"]
    assert isinstance(produced, list)
    assert {entry["topic"] for entry in produced} == {
        "onex.evt.omnimarket.host-disk-low-reported.v1",
        "onex.evt.omnimarket.host-disk-evict-completed.v1",
        "onex.evt.omnimarket.host-disk-evict-failed.v1",
    }
    assert all(entry["producer"].strip() for entry in produced)
