# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Vendoring smoke test for ``compute_golden.py`` (OMN-14368).

``compute_golden.py`` (OMN-14353, core PR #1425) is a pure-stdlib equivalence
oracle with zero omnibase_core imports — vendored byte-identical into
``omnimarket/scripts/ci/compute_golden.py`` so the RSD mechanical-wave rewrite
can record/replay goldens for omnimarket COMPUTE nodes without an
omnibase_core runtime dependency. This test proves the vendored copy is
importable from an omnimarket worktree and that the record -> replay ->
compare_output round trip actually discriminates a regression (not just "a
file exists") — mirroring the intent of core's
``tests/unit/canary/test_routing_authority_golden_canary.py`` with a minimal
local model instead of a real node, since vendoring the oracle does not vendor
any specific handler.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from scripts.ci.compute_golden import (
    compare_output,
    outputs_equivalent,
    record_golden,
)

pytestmark = pytest.mark.unit


class _ModelSmokeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    value: int


class _ModelSmokeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    doubled: int
    correlation_id: str = "unused"


def _handle(inp: _ModelSmokeInput) -> _ModelSmokeOutput:
    """Deterministic stand-in for a pure COMPUTE handler."""
    return _ModelSmokeOutput(doubled=inp.value * 2)


def test_record_then_replay_is_equivalent() -> None:
    """A recorded golden replays clean against the (unchanged) handler."""
    inp = _ModelSmokeInput(value=21)
    out = _handle(inp)
    golden = record_golden(input_model=inp, output=out, volatile_mask=[])
    replayed_inp = _ModelSmokeInput.model_validate(golden["input"])
    assert compare_output(golden, _handle(replayed_inp)) == []


def test_comparator_discriminates_a_regression() -> None:
    """THE oracle proof: a behavior change in the output is caught, not vacuous."""
    inp = _ModelSmokeInput(value=21)
    golden = record_golden(input_model=inp, output=_handle(inp), volatile_mask=[])
    golden["output"]["doubled"] = 999  # simulate a regressed value
    diffs = compare_output(golden, _handle(inp))
    assert diffs, (
        "comparator MUST catch the regressed value (oracle is vacuous otherwise)"
    )
    assert any("doubled" in d for d in diffs)


def test_volatile_mask_excludes_declared_fields() -> None:
    """A field named in the volatile mask is excluded from the compare."""
    inp = _ModelSmokeInput(value=21)
    golden = record_golden(
        input_model=inp, output=_handle(inp), volatile_mask=["correlation_id"]
    )
    golden["output"]["correlation_id"] = "corrupted"
    assert compare_output(golden, _handle(inp)) == []


def test_outputs_equivalent_is_deterministic() -> None:
    inp = _ModelSmokeInput(value=21)
    out1, out2 = _handle(inp), _handle(inp)
    assert outputs_equivalent(out1, out2, volatile_mask=[])


def test_apply_mask_is_pure_stdlib() -> None:
    """Guard against a future edit reintroducing an omnibase_core import — the
    whole point of vendoring is zero core coupling."""
    import scripts.ci.compute_golden as vendored

    assert vendored.__file__ is not None
    assert "omnibase_core" not in vendored.__file__

    import ast
    from pathlib import Path

    tree = ast.parse(Path(vendored.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"json", "typing", "__future__"}, imported
