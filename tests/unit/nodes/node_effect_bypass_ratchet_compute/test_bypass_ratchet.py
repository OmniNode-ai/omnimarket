# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The effect-node bypass ratchet compute (OMN-24432): golden and error chain event tests and the failing control.

Follows node_join_refusal_effect's test shape. Golden: a count at its baseline is ok and the completed event round-trips.
Error: a rise, a repository with no baseline row and a baseline that grew past the merge base's copy each refuse; a fall passes
and says it is lowerable. Failing control: the same scan is ok at baseline N and refused at baseline N-1.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_effect_bypass_ratchet_compute.handlers.handler_bypass_ratchet import (
    HandlerBypassRatchet,
)
from omnimarket.nodes.node_effect_bypass_ratchet_compute.models import (
    EnumBypassCategory,
    EnumBypassFinding,
    ModelBypassBaseline,
    ModelBypassCounts,
    ModelBypassInventory,
    ModelBypassRatchetRequest,
    ModelBypassRatchetVerdict,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_effect_bypass_ratchet_compute/contract.yaml"
)
GH = EnumBypassCategory.GH_CLI
SSH = EnumBypassCategory.HOST_SSH


def _counts(**by_category: int) -> ModelBypassCounts:
    return ModelBypassCounts(
        counts={EnumBypassCategory(k): v for k, v in by_category.items()}
    )


def _baseline(repo: str = "demo", **by_category: int) -> ModelBypassBaseline:
    return ModelBypassBaseline(
        schema_version=1,
        ticket="OMN-24432",
        repositories={repo: _counts(**by_category)},
    )


def _inventory(repo: str = "demo", **by_category: int) -> ModelBypassInventory:
    return ModelBypassInventory(
        repository=repo, files_scanned=3, counts=_counts(**by_category)
    )


def _run(
    inventory: ModelBypassInventory,
    baseline: ModelBypassBaseline,
    base: ModelBypassBaseline | None = None,
) -> ModelBypassRatchetVerdict:
    return HandlerBypassRatchet().handle(
        ModelBypassRatchetRequest(
            inventory=inventory, baseline=baseline, base_baseline=base
        )
    )


def test_golden_count_at_baseline_is_ok_and_the_event_round_trips() -> None:
    request = (_inventory(gh_cli=4, host_ssh=2), _baseline(gh_cli=4, host_ssh=2))
    verdict = _run(*request)
    assert verdict.ok is True
    assert verdict.findings == ()
    assert (verdict.total_now, verdict.total_baseline) == (6, 6)
    assert [r.category for r in verdict.categories] == list(EnumBypassCategory)
    assert _run(*request) == verdict
    event = verdict.model_dump(mode="json")
    assert ModelBypassRatchetVerdict.model_validate(event) == verdict


def test_error_a_rise_is_refused_and_names_the_category() -> None:
    verdict = _run(_inventory(gh_cli=5, host_ssh=2), _baseline(gh_cli=4, host_ssh=2))
    assert verdict.ok is False
    assert verdict.findings == (EnumBypassFinding.CATEGORY_ROSE,)
    assert [r.category for r in verdict.categories if r.rose] == [GH]


def test_error_a_repository_without_a_baseline_row_is_refused() -> None:
    verdict = _run(_inventory(repo="other", gh_cli=0), _baseline(gh_cli=0))
    assert verdict.ok is False
    assert verdict.findings == (EnumBypassFinding.REPOSITORY_NOT_IN_BASELINE,)
    assert verdict.categories == ()


def test_error_a_baseline_above_the_merge_base_is_refused() -> None:
    verdict = _run(
        _inventory(gh_cli=3),
        _baseline(gh_cli=6),
        base=_baseline(gh_cli=4),
    )
    assert verdict.ok is False
    assert EnumBypassFinding.BASELINE_GREW_VS_BASE in verdict.findings


def test_error_a_new_baseline_row_with_counts_is_refused_against_the_base() -> None:
    verdict = _run(
        _inventory(repo="new", gh_cli=1),
        _baseline(repo="new", gh_cli=1),
        base=_baseline(repo="demo", gh_cli=1),
    )
    assert verdict.ok is False
    assert EnumBypassFinding.BASELINE_GREW_VS_BASE in verdict.findings


def test_a_fall_passes_and_is_lowerable() -> None:
    verdict = _run(
        _inventory(gh_cli=3, host_ssh=2),
        _baseline(gh_cli=4, host_ssh=2),
        base=_baseline(gh_cli=4, host_ssh=2),
    )
    assert verdict.ok is True
    assert verdict.findings == (EnumBypassFinding.BASELINE_LOWERABLE,)


def test_control_rise_is_caught_and_equal_is_not() -> None:
    """The injected break: the baseline lowered by one turns the same scan from ok to refused."""
    inventory = _inventory(gh_cli=4, host_ssh=2)
    assert _run(inventory, _baseline(gh_cli=4, host_ssh=2)).ok is True
    broken = _run(inventory, _baseline(gh_cli=3, host_ssh=2))
    assert broken.ok is False
    assert broken.findings == (EnumBypassFinding.CATEGORY_ROSE,)


def test_contract_names_a_handler_and_models_that_resolve() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    handler = contract["handler"]
    assert getattr(importlib.import_module(handler["module"]), handler["class"]) is (
        HandlerBypassRatchet
    )
    for key in ("input_model", "output_model"):
        model = contract[key]
        assert hasattr(importlib.import_module(model["module"]), model["name"])
