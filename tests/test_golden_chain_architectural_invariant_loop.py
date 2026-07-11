# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden-chain proof for node_architectural_invariant_loop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_architectural_invariant_loop import (
    ArchInvariantLoopRequest,
    NodeArchitecturalInvariantLoop,
)


class _RecordingPublisher:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def publish(self, *, topic: str, key: bytes | None, value: bytes) -> None:
        self.events.append({"topic": topic, "value": json.loads(value.decode())})


@pytest.mark.integration
def test_golden_chain_arch006_governance_record_is_counted_without_violation(
    tmp_path: Path,
) -> None:
    target = tmp_path / "omnimarket"
    src = target / "src"
    src.mkdir(parents=True)
    (src / "clean_module.py").write_text(
        '"""No architectural invariant violations."""\n\nVALUE = 1\n',
        encoding="utf-8",
    )

    bus = _RecordingPublisher()
    result = NodeArchitecturalInvariantLoop(bus).handle(
        ArchInvariantLoopRequest(
            target_dirs=[str(target)],
            invariant_ids=["ARCH-006"],
        )
    )

    assert result.invariants_evaluated == 1
    assert result.violations == []
    assert result.summary["total_violations"] == 0
    assert result.summary["by_principle"] == {}
    assert [event["value"]["event_type"] for event in bus.events] == [
        "arch_invariant_loop_completed"
    ]
    assert bus.events[0]["value"]["invariants_evaluated"] == 1
