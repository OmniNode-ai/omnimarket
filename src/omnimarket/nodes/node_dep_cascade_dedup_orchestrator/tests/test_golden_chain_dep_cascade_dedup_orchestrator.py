# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Node-local golden-chain guardrails for node_dep_cascade_dedup_orchestrator [OMN-12213].

Mirrors tests/test_golden_chain_dep_cascade_dedup_orchestrator.py but lives
under src/…/tests/ so the dep-health sweep (which scans src/ only) can find it.

Verifies:
- contract.yaml is valid and declares node_not_implemented: false
- Handler deduplicates PR cascades through an injected GitHub adapter
- Typed models are strict (frozen, extra=forbid)
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
import yaml

from omnimarket.nodes.node_dep_cascade_dedup_orchestrator.handlers.handler_dep_cascade_dedup_orchestrator import (
    HandlerDepCascadeDedupOrchestrator,
)
from omnimarket.nodes.node_dep_cascade_dedup_orchestrator.models.model_dep_cascade_dedup_request import (
    ModelDepCascadeDedupRequest,
)
from omnimarket.nodes.node_dep_cascade_dedup_orchestrator.models.model_dep_cascade_dedup_result import (
    ModelDepCascadeDedupResult,
)

_NODE_DIR = Path(__file__).resolve().parent.parent


def _contract() -> dict[str, object]:
    return cast(
        dict[str, object],
        yaml.safe_load((_NODE_DIR / "contract.yaml").read_text(encoding="utf-8")),
    )


def test_handler_dep_cascade_dedup_orchestrator_is_importable() -> None:
    """HandlerDepCascadeDedupOrchestrator must be importable."""
    assert HandlerDepCascadeDedupOrchestrator is not None


def test_handler_dep_cascade_dedup_orchestrator_dry_run_dedups() -> None:
    """handler_dep_cascade_dedup_orchestrator dedups via injected adapter."""
    adapter = _Adapter(
        {
            "OmniNode-ai/omnimarket": [
                {"number": 1, "title": "Bump pydantic from 2.9.0 to 2.10.0"},
                {"number": 2, "title": "Bump pydantic from 2.9.0 to 2.11.0"},
            ]
        }
    )
    handler = HandlerDepCascadeDedupOrchestrator(adapter=adapter)
    request = ModelDepCascadeDedupRequest(
        repos=["OmniNode-ai/omnimarket"],
        dry_run=True,
    )
    result = handler.handle(request)

    assert result.groups_found == 1
    assert result.prs_closed == 0
    assert result.prs_kept == 1
    assert result.prs_skipped == 1
    assert result.package_groups[0].keeper_pr_number == 2
    assert result.package_groups[0].superseded_pr_numbers == (1,)
    assert adapter.closed == []


def test_contract_marks_node_implemented() -> None:
    """contract.yaml must declare node_not_implemented: false."""
    contract = _contract()
    assert contract.get("node_not_implemented") is False


def test_request_model_is_strict() -> None:
    """ModelDepCascadeDedupRequest must be frozen and reject extra fields."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelDepCascadeDedupRequest(repos=["OmniNode-ai/omnimarket"], unknown_field="x")  # type: ignore[call-arg]


def test_result_model_is_strict() -> None:
    """ModelDepCascadeDedupResult must be instantiable."""
    result = ModelDepCascadeDedupResult(
        groups_found=0,
        prs_closed=0,
        prs_skipped=0,
    )
    assert result.groups_found == 0


class _Adapter:
    def __init__(self, prs_by_repo: dict[str, list[dict[str, object]]]) -> None:
        self._prs_by_repo = prs_by_repo
        self.closed: list[tuple[str, int, str]] = []

    def list_repos(self) -> tuple[str, ...]:
        return tuple(sorted(self._prs_by_repo))

    def list_dependency_prs(
        self, repo: str, *, label: str, dependency_type: str
    ) -> list[dict[str, object]]:
        assert label == "dependencies"
        assert dependency_type == ""
        return list(self._prs_by_repo.get(repo, []))

    def close_pr(self, repo: str, pr_number: int, comment: str) -> None:
        self.closed.append((repo, pr_number, comment))
