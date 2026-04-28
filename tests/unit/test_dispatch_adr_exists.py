"""Phase 1 Task 1 (OMN-10178): assert the foreground-only Agent() ADR exists.

The ADR locks in Pattern A (foreground-direct) as the default and Pattern B
(broker-mediated) as deferred for `Agent()` spawn. Subsequent skills-to-market
migration tasks inherit this decision.

Test is co-located in omnimarket so it gates omnimarket CI without a cross-repo
dependency on omniclaude (deviation from plan literal location:
omniclaude/tests/unit/test_dispatch_adr_exists.py).
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
    assert "Pattern A" in adr
    assert "Pattern B" in adr
    assert "foreground" in adr.lower()
    assert "deferred" in adr.lower() or "broker" in adr.lower()


def test_adr_cites_2026_04_27_live_evidence() -> None:
    adr = _ADR_PATH.read_text()
    assert "2026-04-27" in adr, "ADR must cite the 2026-04-27 live evidence"
