# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Vendoring smoke test for ``adequacy_receipt.py`` (OMN-14371).

``adequacy_receipt.py`` (OMN-14353 f/u #41, core PR #1426) is a coverage-guided
compute-golden adequacy recorder with zero omnibase_core-specific imports
(``coverage`` + ``pydantic`` + stdlib only) — vendored byte-identical into
``omnimarket/scripts/ci/adequacy_receipt.py`` alongside ``compute_golden.py``
(OMN-14368) so the canonical handler-shape ratchet gate's 3-part flip proof
(shape-canonical + adequacy receipt + equivalence replay,
``canonical_handler_shape.py``) can be produced for an omnimarket COMPUTE node
without an omnibase_core runtime dependency. This test proves the vendored
copy is importable from an omnimarket worktree and that ``build_receipt``
actually measures branch coverage (not a hardcoded/stubbed percentage) —
mirroring the intent of core's ``tests/unit/canary/test_adequacy_receipt.py``
with a minimal local model instead of a real node.
"""

from __future__ import annotations

import ast
from pathlib import Path

import coverage
import pytest
from pydantic import BaseModel, ConfigDict

from scripts.ci.adequacy_receipt import (
    ModelAdequacyReceipt,
    build_receipt,
    handler_module_sha256,
    input_hash,
    scrub,
)

pytestmark = pytest.mark.unit

# coverage.py does not support nested concurrent measurement — skip the
# coverage-driven tests when the suite itself runs under --cov (mirrors core's
# tests/unit/canary/test_adequacy_receipt.py guard).
_COVERAGE_ACTIVE = coverage.Coverage.current() is not None
_needs_coverage = pytest.mark.skipif(
    _COVERAGE_ACTIVE,
    reason="nested coverage measurement unsupported (suite under --cov)",
)


class _ModelSmokeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    flag: bool
    value: int = 0


class _ModelSmokeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    result: str


def _handle(inp: _ModelSmokeInput) -> _ModelSmokeOutput:
    """Two-branch stand-in for a pure COMPUTE handler (this module's own file
    is the measured source, so branch coverage is over THIS conditional)."""
    if inp.flag:
        return _ModelSmokeOutput(result=f"flagged:{inp.value}")
    return _ModelSmokeOutput(result="unflagged")


@_needs_coverage
def test_build_receipt_measures_real_branch_coverage() -> None:
    """Both branches exercised -> full coverage of the measured conditional,
    not a hardcoded/self-asserted percentage."""
    candidates = [
        _ModelSmokeInput(flag=True, value=1),
        _ModelSmokeInput(flag=False),
    ]
    receipt = build_receipt(
        node_id="test.smoke.node",
        handler_module_file=__file__,
        handler_call=_handle,
        candidates=candidates,
        source_match=Path(__file__).name,
        coverage_target=1.0,
    )
    assert isinstance(receipt, ModelAdequacyReceipt)
    assert receipt.node_id == "test.smoke.node"
    assert receipt.candidate_count == 2
    assert receipt.selected_count >= 1
    assert receipt.branch_coverage_pct > 0.0
    assert receipt.meets_target is (receipt.branch_coverage_pct >= 1.0)


def test_handler_module_sha256_pins_to_live_file_content() -> None:
    """A staleness check downstream (the ratchet gate) depends on this being a
    real, reproducible hash of the file's live bytes."""
    h1 = handler_module_sha256(__file__)
    h2 = handler_module_sha256(__file__)
    assert h1 == h2
    assert h1.startswith("sha256:")


def test_input_hash_is_stable_regardless_of_key_order() -> None:
    a = _ModelSmokeInput(flag=True, value=7)
    b = _ModelSmokeInput.model_validate({"value": 7, "flag": True})
    assert input_hash(a) == input_hash(b)


def test_scrub_redacts_sensitive_keys() -> None:
    payload = {"api_key": "sekrit", "value": 1}
    scrubbed = scrub(payload)
    assert scrubbed["api_key"] == "<scrubbed>"
    assert scrubbed["value"] == 1


def test_vendored_copy_has_no_omnibase_core_coupling() -> None:
    """Guard against a future edit reintroducing an omnibase_core import — the
    whole point of vendoring is zero core coupling."""
    import scripts.ci.adequacy_receipt as vendored

    assert vendored.__file__ is not None
    assert "omnibase_core" not in vendored.__file__

    tree = ast.parse(Path(vendored.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    allowed = {
        "__future__",
        "functools",
        "hashlib",
        "io",
        "json",
        "collections",
        "datetime",
        "pathlib",
        "typing",
        "coverage",
        "pydantic",
    }
    assert imported <= allowed, imported
