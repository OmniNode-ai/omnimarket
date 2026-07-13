# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Mandatory RED-proof tests for the compliance_sweep class fix (OMN-14541,
parent OMN-14531 — "16/16 sweeps structurally blind").

Class defect: the census input (target_dirs/repos/checks) arrived with
empty/None defaults no harness populated, and the roll-up computed
``bad_count == 0 -> compliant`` without ever asserting real scan coverage.
"Scanned nothing" and "all healthy" were arithmetically identical. On top of
that, compliance_sweep advertised a "missing-routing" check (in
``ALL_CHECKS``, the node docstring, and contract.yaml) with ZERO
implementation — no code path ever emitted ``MISSING_HANDLER_ROUTING``.

This module proves both halves of the fix:

1. SCOPE-EMPTY RED: an unresolvable/empty scan scope must be refused
   (``status="error"``), never reported ``compliant`` (Rule 2 of the class
   fix — "Assert scanned_count > 0 before any green verdict").
2. EXISTS-but-WRONG RED / GREEN: a real fixture node whose canonical handler
   is absent from its own ``handler_routing`` table must flip the sweep to
   ``violations_found`` with a ``MISSING_HANDLER_ROUTING`` violation and
   ``contracts_checked > 0`` — and the mirror-image correctly-routed fixture
   must report ``compliant`` with the same non-zero scan coverage. A green
   verdict achieved only by never implementing the check (the pre-fix state)
   is not acceptable — this is the "prove RED against EXISTS-but-WRONG"
   requirement, not a vacuous green-on-absence.

A real-scale regression (GREEN against the genuinely-healthy omnimarket
source tree) closes the loop: the check must not produce false positives at
the scale it will actually run at once wired as a CI gate.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from omnimarket.nodes.node_compliance_sweep.handlers.handler_compliance_sweep import (
    ComplianceSweepRequest,
    NodeComplianceSweep,
)

_ROUTED_HANDLER_SRC = (
    "class HandlerRealWork:\n    def handle(self, request):\n        return request\n"
)


def _make_missing_routing_node(root: Path, *, routed: bool) -> str:
    """Build a single-node repo tree with a contract.yaml + handler file.

    When ``routed=False`` the contract's top-level ``handler:`` (the
    canonical handler ``RuntimeLocal`` resolves for dispatch) is declared
    but is absent from the ``handler_routing.handlers`` operation_match
    table AND has no ``default_handler`` fallback — a real
    MISSING_HANDLER_ROUTING defect: the declared handler can never be
    reached for any operation. When ``routed=True`` the same handler is
    present in the routing table, mirroring a genuinely healthy node.
    """
    node_dir = root / "myrepo" / "src" / "nodes" / "node_orphan_handler"
    handlers_dir = node_dir / "handlers"
    handlers_dir.mkdir(parents=True)
    (handlers_dir / "handler_real_work.py").write_text(_ROUTED_HANDLER_SRC)

    if routed:
        routing_block = (
            "handler_routing:\n"
            "  routing_strategy: operation_match\n"
            "  handlers:\n"
            "    - operation: do_work\n"
            "      handler:\n"
            "        name: HandlerRealWork\n"
            "        module: nodes.node_orphan_handler.handlers.handler_real_work\n"
        )
    else:
        routing_block = (
            "handler_routing:\n"
            "  routing_strategy: operation_match\n"
            "  handlers:\n"
            "    - operation: unrelated_operation\n"
            "      handler:\n"
            "        name: HandlerSomeoneElse\n"
            "        module: nodes.node_orphan_handler.handlers.handler_someone_else\n"
        )

    contract = (
        "name: node_orphan_handler\n"
        "node_type: compute\n"
        "handler:\n"
        "  module: nodes.node_orphan_handler.handlers.handler_real_work\n"
        "  class: HandlerRealWork\n"
        f"{routing_block}"
    )
    (node_dir / "contract.yaml").write_text(contract)
    return str(root / "myrepo")


@pytest.mark.integration
class TestScopeEmptyRedProof:
    """RED against an unresolvable/empty scope — must refuse, never PASS."""

    def test_nonexistent_target_dir_is_refused_not_compliant(
        self, tmp_path: Path
    ) -> None:
        """A single nonexistent target_dirs entry must flip to status=error.

        Pre-fix: ``target.is_dir()`` is False so the for-loop silently
        ``continue``s; ``handlers_scanned`` stays 0 and line 263 only checked
        ``if not violations`` — an empty violations list over zero scanned
        handlers reported ``status="compliant"``.
        """
        result = NodeComplianceSweep().handle(
            ComplianceSweepRequest(target_dirs=[str(tmp_path / "does-not-exist")])
        )

        assert result.status == "error", (
            "an unresolvable target dir must be refused, not reported "
            f"compliant (got status={result.status!r})"
        )
        assert result.status != "compliant"
        assert result.scanned_count == 0
        assert result.scan_error is not None
        assert "unresolvable" in result.scan_error.lower()

    def test_nonexistent_repo_name_is_refused_not_compliant(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An explicit ``repos=[...]`` that resolves to nothing must refuse."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))

        result = NodeComplianceSweep().handle(
            ComplianceSweepRequest(repos=["nonexistent_repo_xyz"])
        )

        assert result.status == "error"
        assert result.status != "compliant"
        assert result.scanned_count == 0
        assert result.scan_error is not None

    def test_scanned_count_property_sums_both_dimensions(self) -> None:
        """``scanned_count`` is the sum of handler-file and contract census."""
        result = NodeComplianceSweep().handle(ComplianceSweepRequest())
        assert (
            result.scanned_count == result.handlers_scanned + result.contracts_checked
        )


@pytest.mark.integration
class TestMissingRoutingRedProof:
    """RED against an EXISTS-but-WRONG scope: a real orphaned handler."""

    def test_orphaned_handler_flips_status_red(self, tmp_path: Path) -> None:
        """A handler declared canonical but absent from its own routing
        table must flip the sweep RED with a real MISSING_HANDLER_ROUTING
        violation — not silently pass because the check was never wired
        (the pre-fix state: zero occurrences of the emitted violation type
        anywhere in the handler)."""
        target = _make_missing_routing_node(tmp_path, routed=False)

        result = NodeComplianceSweep().handle(
            ComplianceSweepRequest(target_dirs=[target], checks=["missing-routing"])
        )

        assert result.status == "violations_found", (
            f"expected RED (violations_found) for an orphaned canonical "
            f"handler, got status={result.status!r}"
        )
        assert result.contracts_checked >= 1
        assert result.by_type.get("MISSING_HANDLER_ROUTING", 0) >= 1
        violation = next(
            v
            for v in result.violations
            if v.violation_type == "MISSING_HANDLER_ROUTING"
        )
        assert violation.node_name == "node_orphan_handler"
        assert violation.severity == "CRITICAL"

    def test_routed_handler_is_green(self, tmp_path: Path) -> None:
        """The mirror-image correctly-routed fixture must report compliant
        with genuine non-zero scan coverage — GREEN only on a healthy scope,
        never on absence of scanning."""
        target = _make_missing_routing_node(tmp_path, routed=True)

        result = NodeComplianceSweep().handle(
            ComplianceSweepRequest(target_dirs=[target], checks=["missing-routing"])
        )

        assert result.status == "compliant"
        assert result.contracts_checked >= 1, "must have actually scanned a contract"
        assert result.by_type.get("MISSING_HANDLER_ROUTING", 0) == 0

    def test_missing_routing_check_excluded_when_not_requested(
        self, tmp_path: Path
    ) -> None:
        """Contracts are still counted toward scanned_count when
        missing-routing is excluded from checks, but no violation fires."""
        target = _make_missing_routing_node(tmp_path, routed=False)

        result = NodeComplianceSweep().handle(
            ComplianceSweepRequest(target_dirs=[target], checks=["hardcoded-topics"])
        )

        assert result.contracts_checked >= 1
        assert result.by_type.get("MISSING_HANDLER_ROUTING", 0) == 0


@pytest.mark.integration
class TestMissingRoutingRealScaleRegression:
    """GREEN-on-populated-real-scope: no false positives against the real
    omnimarket source tree at the scale the CI gate will actually run at
    (OMN-14541 fix_plan step 5)."""

    def test_omnimarket_source_tree_has_zero_false_positive_routing_gaps(
        self, tmp_path: Path
    ) -> None:
        omnimarket_root = Path(__file__).resolve().parents[3]
        assert (omnimarket_root / "src" / "omnimarket").is_dir(), (
            f"expected to resolve the omnimarket repo root, got {omnimarket_root}"
        )

        # Copy the real source tree into a clean tmp location so the scan
        # scope never contains an "omni_worktrees" path segment — the sweep
        # deliberately excludes that segment to avoid double-counting a
        # worktree copy nested *inside* a scanned canonical repo (OMN-13514),
        # which would otherwise blank out this entire regression when run
        # from a ticket worktree under $OMNI_HOME/omni_worktrees/.
        scan_root = tmp_path / "omnimarket_src_copy"
        shutil.copytree(
            omnimarket_root / "src",
            scan_root,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

        result = NodeComplianceSweep().handle(
            ComplianceSweepRequest(
                target_dirs=[str(scan_root)],
                checks=["missing-routing"],
            )
        )

        assert result.contracts_checked > 0, (
            "must have scanned real contract.yaml files, not an empty scope"
        )
        missing_routing = [
            v
            for v in result.violations
            if v.violation_type == "MISSING_HANDLER_ROUTING"
        ]
        assert missing_routing == [], (
            "false-positive MISSING_HANDLER_ROUTING findings against the "
            f"real, healthy omnimarket tree: {missing_routing}"
        )
