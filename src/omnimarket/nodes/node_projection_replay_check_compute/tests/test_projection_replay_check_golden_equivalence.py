# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-equivalence proof for the check() -> handle() rename (OMN-14371).

RSD mechanical-wave canary: the FIRST real flip of the canonical
handler-shape ratchet (OMN-14355 / core #1428) outside ``omnibase_core``.

The goldens under
``tests/fixtures/golden/node_projection_replay_check_compute/*.json`` were
recorded from the LEGACY ``HandlerProjectionReplayCheck.check(...)`` method
BEFORE the rename, via ``scripts/ci/compute_golden.record_golden`` (vendored,
OMN-14368). This test proves the rename to ``handle`` is behavior-preserving:
it replays every recorded golden's serialized input through the NEW
``handle()`` method and asserts a byte-equivalent (empty-diff) output. A
non-empty diff means the codemod changed behavior, not just the method name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_projection_replay_check_compute.handlers.handler_projection_replay_check import (
    HandlerProjectionReplayCheck,
)
from omnimarket.nodes.node_projection_replay_check_compute.models.model_replay_check import (
    ModelReplayCheckRequest,
)
from scripts.ci.compute_golden import compare_output

pytestmark = pytest.mark.unit

_GOLDEN_DIR = (
    Path(__file__).resolve().parents[5]
    / "tests"
    / "fixtures"
    / "golden"
    / "node_projection_replay_check_compute"
)


def _golden_files() -> list[Path]:
    files = sorted(_GOLDEN_DIR.glob("*.json"))
    assert files, f"no golden fixtures found under {_GOLDEN_DIR}"
    return files


@pytest.mark.parametrize("golden_path", _golden_files(), ids=lambda p: p.stem)
def test_handle_reproduces_legacy_check_golden(golden_path: Path) -> None:
    """``handle()`` on the recorded legacy input == the recorded legacy output."""
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    replayed_input = ModelReplayCheckRequest.model_validate(golden["input"])
    handler = HandlerProjectionReplayCheck()
    fresh_output = handler.handle(replayed_input)
    diffs = compare_output(golden, fresh_output)
    assert diffs == [], (
        f"{golden_path.name}: check()->handle() rename changed behavior: {diffs}"
    )


def test_golden_fixture_count_matches_expected_candidate_pool() -> None:
    """Regression guard: the recorded pool has a known, reviewed size."""
    assert len(_golden_files()) == 7
