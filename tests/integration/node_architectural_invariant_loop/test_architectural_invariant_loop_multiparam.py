# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# test-literal-ok: the synthetic source fixtures below intentionally embed an
# absolute /Users/ path as the ARCH-005 negative control — it is fixture content
# fed to the checker, not a real runtime path.
"""Multi-parameter integration proof for node_architectural_invariant_loop.

OMN-13680, WS5 Wave 6. Variant A — direct in-process handler call. The handler
is a pure COMPUTE that scans ``*.py`` files under target dirs and applies the
seed ARCH invariant contracts. The repo snapshot is mocked as a synthetic source
tree under ``tmp_path``; the event-bus boundary is satisfied by an injected
recording publisher (constructor seam) — no live bus.

Each case parametrizes a distinct source-tree / flag combination and asserts the
typed ``violations`` list, the ``summary`` aggregation, and the recorded bus
events. The cases with a planted bad file (hardcoded path, hardcoded topic) are
the negative controls: a known-bad fixture must produce a violation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_architectural_invariant_loop.handlers.handler_architectural_invariant_loop import (
    ArchInvariantLoopRequest,
    NodeArchitecturalInvariantLoop,
)


class _RecordingPublisher:
    """Records bus publishes synchronously (no Kafka)."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def publish(self, *, topic: str, key: bytes | None, value: bytes) -> None:
        self.events.append({"topic": topic, "value": json.loads(value.decode())})


# Source fixtures. File stem matters for some checkers (runner/orchestrator).
_CLEAN_SRC = '''\
"""A clean module — no invariant violations."""


def add(a: int, b: int) -> int:
    return a + b
'''

# ARCH-005: hardcoded absolute path.
_BAD_PATH_SRC = """\
def load() -> str:
    return open("/Users/someone/secret.txt").read()
"""

# ARCH-003: hardcoded canonical topic string in a non-suppressed module.
_BAD_TOPIC_SRC = """\
def publish_it(bus: object) -> None:
    bus.publish("onex.cmd.omnimarket.do-thing.v1")
"""


def _make_target(root: Path, files: dict[str, str]) -> Path:
    target = root / "omnimarket"
    src = target / "src"
    src.mkdir(parents=True, exist_ok=True)
    for filename, text in files.items():
        (src / filename).write_text(text, encoding="utf-8")
    return target


@pytest.mark.integration
def test_arch_clean_tree_no_violations(tmp_path: Path) -> None:
    target = _make_target(tmp_path, {"clean.py": _CLEAN_SRC})
    bus = _RecordingPublisher()
    result = NodeArchitecturalInvariantLoop(bus).handle(
        ArchInvariantLoopRequest(target_dirs=[str(target)])
    )
    assert result.violations == []
    assert result.summary["total_violations"] == 0
    assert result.invariants_evaluated == 5
    # Only the completion event is published when there are no violations.
    assert any(
        e["value"].get("event_type") == "arch_invariant_loop_completed"
        for e in bus.events
    )


@pytest.mark.integration
def test_arch_hardcoded_path_is_violation(tmp_path: Path) -> None:
    """Negative control: a hardcoded /Users/ path trips ARCH-005."""
    target = _make_target(tmp_path, {"loader.py": _BAD_PATH_SRC})
    bus = _RecordingPublisher()
    result = NodeArchitecturalInvariantLoop(bus).handle(
        ArchInvariantLoopRequest(target_dirs=[str(target)])
    )
    codes = {v.principle_code for v in result.violations}
    assert "ARCH-005" in codes
    arch5 = [v for v in result.violations if v.principle_code == "ARCH-005"]
    assert arch5[0].severity == "ERROR"
    assert arch5[0].path == "src/loader.py"
    # A violation event was published to the violation topic.
    assert any(
        e["value"].get("event_type") == "arch_invariant_violation" for e in bus.events
    )


@pytest.mark.integration
def test_arch_hardcoded_topic_is_violation(tmp_path: Path) -> None:
    """Negative control: a hardcoded canonical topic trips ARCH-003."""
    target = _make_target(tmp_path, {"emitter.py": _BAD_TOPIC_SRC})
    result = NodeArchitecturalInvariantLoop(_RecordingPublisher()).handle(
        ArchInvariantLoopRequest(target_dirs=[str(target)])
    )
    codes = {v.principle_code for v in result.violations}
    assert "ARCH-003" in codes


@pytest.mark.integration
def test_arch_invariant_id_filter_scopes_checks(tmp_path: Path) -> None:
    """invariant_ids restricts evaluation to a single principle."""
    target = _make_target(
        tmp_path, {"loader.py": _BAD_PATH_SRC, "emitter.py": _BAD_TOPIC_SRC}
    )
    result = NodeArchitecturalInvariantLoop(_RecordingPublisher()).handle(
        ArchInvariantLoopRequest(target_dirs=[str(target)], invariant_ids=["ARCH-005"])
    )
    codes = {v.principle_code for v in result.violations}
    assert codes == {"ARCH-005"}
    assert result.invariants_evaluated == 1


@pytest.mark.integration
def test_arch_waiver_suppresses_violation(tmp_path: Path) -> None:
    """A matching waiver key marks the violation waived (kept, not dropped)."""
    target = _make_target(tmp_path, {"loader.py": _BAD_PATH_SRC})
    result = NodeArchitecturalInvariantLoop(_RecordingPublisher()).handle(
        ArchInvariantLoopRequest(
            target_dirs=[str(target)],
            invariant_ids=["ARCH-005"],
            waived=["ARCH-005:src/loader.py"],
        )
    )
    arch5 = [v for v in result.violations if v.principle_code == "ARCH-005"]
    assert len(arch5) == 1
    assert arch5[0].waived is True
    assert result.summary["waived_violations"] == 1


@pytest.mark.integration
def test_arch_critical_threshold_filters_error_violations(tmp_path: Path) -> None:
    """severity_threshold=CRITICAL drops ERROR-level violations entirely."""
    target = _make_target(tmp_path, {"loader.py": _BAD_PATH_SRC})
    result = NodeArchitecturalInvariantLoop(_RecordingPublisher()).handle(
        ArchInvariantLoopRequest(
            target_dirs=[str(target)], severity_threshold="CRITICAL"
        )
    )
    assert result.violations == []


@pytest.mark.integration
def test_arch_dry_run_publishes_no_events(tmp_path: Path) -> None:
    """dry_run=True computes violations but publishes nothing to the bus."""
    target = _make_target(tmp_path, {"loader.py": _BAD_PATH_SRC})
    bus = _RecordingPublisher()
    result = NodeArchitecturalInvariantLoop(bus).handle(
        ArchInvariantLoopRequest(target_dirs=[str(target)], dry_run=True)
    )
    assert any(v.principle_code == "ARCH-005" for v in result.violations)
    assert bus.events == []
    assert result.summary["dry_run"] is True
