"""Foreground-only Agent() ADR presence + structural assertions.

The ADR locks in Pattern A (foreground-direct) as the default and Pattern B
(broker-mediated) as deferred for `Agent()` spawn. Subsequent skill-backing
nodes inherit this decision.
"""

from __future__ import annotations

from pathlib import Path

_ADR_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "decisions"
    / "adr-dispatch-architecture-foreground-only-agent-call.md"
)


def test_foreground_only_adr_exists() -> None:
    assert _ADR_PATH.is_file(), f"ADR must exist at: {_ADR_PATH}"


def test_adr_declares_pattern_a_default_and_pattern_b_deferred() -> None:
    adr = _ADR_PATH.read_text()
    assert "### Pattern A — Direct foreground (default)" in adr
    assert "### Pattern B — Broker-mediated (DEFERRED)" in adr


def test_adr_states_foreground_only_invariants() -> None:
    adr = _ADR_PATH.read_text()
    assert "No handler, worker, subagent, or hook may call `Agent()`." in adr
    assert "The foreground context is the sole owner of `Agent()`." in adr
