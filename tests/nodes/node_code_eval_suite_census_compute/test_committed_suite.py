# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The committed suite document is a valid request and the node's census matches an independent recount of its raw rows."""

from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path

import pytest

from omnimarket.models.code_eval_suite.model_suite_census_request import (
    ModelCodeEvalSuiteCensusRequest,
)
from omnimarket.models.code_eval_suite.model_suite_census_result import (
    ModelCodeEvalSuiteCensus,
)
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.nodes.node_code_contract_score_effect.models import (
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
)
from omnimarket.nodes.node_code_eval_suite_census_compute.handlers.handler_code_eval_suite_census import (
    HandlerCodeEvalSuiteCensus,
)
from tests.nodes.node_code_eval_suite_census_compute.support import overlay

pytestmark = pytest.mark.unit

SUITE = (
    Path(__file__).resolve().parents[3]
    / "benchmarks"
    / "code_eval_suite"
    / "suite.v1.json"
)

PASS = EnumCodeScoreVerdict.PASS
FAIL_CHECK = EnumCodeScoreVerdict.FAIL_CHECK
UNSCORABLE = EnumCodeScoreVerdict.UNSCORABLE
FALSIFIER_NOT_HELD = EnumCodeScoreSource.FALSIFIER_NOT_HELD


def _raw() -> dict[str, object]:
    return json.loads(SUITE.read_text(encoding="utf-8"))


def _request() -> ModelCodeEvalSuiteCensusRequest:
    return ModelCodeEvalSuiteCensusRequest.model_validate(_raw())


def _handle(request: ModelCodeEvalSuiteCensusRequest) -> ModelCodeEvalSuiteCensus:
    return HandlerCodeEvalSuiteCensus(overlay=overlay()).handle(request)


def _recount(raw: dict[str, object]) -> dict[str, object]:
    """Recount the raw rows with plain dict/Counter code, no node code."""
    tasks = raw["tasks"]
    pre = raw.get("pre_control_exclusions", [])
    by_band: dict[str, int] = {"S": 0, "M": 0, "L": 0}
    by_repo: dict[str, int] = {}
    by_band_repo: dict[tuple[str, str], int] = {}
    excluded: Counter[str] = Counter()
    candidates: list[tuple[str, str, str]] = []
    for t in tasks:
        control = t["control"]
        if control == "pass":
            band = t["band"]
            repo = t["repository"]
            by_band[band] = by_band.get(band, 0) + 1
            by_repo[repo] = by_repo.get(repo, 0) + 1
            key = (band, repo)
            by_band_repo[key] = by_band_repo.get(key, 0) + 1
            candidates.append((t["repository"], t["merge_commit"], t["contract_id"]))
        elif control == "unscorable":
            excluded[f"control:unscorable:{t['control_source']}"] += 1
        else:
            excluded[f"control:{control}"] += 1
    for e in pre:
        excluded[f"pre_control:{e['reason']}"] += e["count"]
    return {
        "by_band": by_band,
        "by_repo": by_repo,
        "by_band_repo": by_band_repo,
        "excluded": dict(excluded),
        "candidates": candidates,
        "candidates_total": len(tasks) + sum(e["count"] for e in pre),
    }


def test_committed_document_validates_as_the_request_model() -> None:
    request = _request()
    assert request.suite_id
    assert len(request.tasks) > 0


def test_node_census_equals_an_independent_recount_of_the_raw_rows() -> None:
    raw = _raw()
    recount = _recount(raw)
    answer = _handle(_request())
    assert answer.failure is None
    assert answer.candidates_total == recount["candidates_total"]
    assert answer.candidates_total == len(raw["tasks"]) + sum(
        e["count"] for e in raw.get("pre_control_exclusions", [])
    )
    node_band = {row.band.value: row.admitted for row in answer.by_band}
    assert node_band == recount["by_band"]
    node_repo = {row.repository: row.admitted for row in answer.by_repository}
    assert node_repo == recount["by_repo"]
    node_pair = {
        (row.band.value, row.repository): row.admitted
        for row in answer.by_band_repository
    }
    assert node_pair == recount["by_band_repo"]
    node_excluded = {row.reason: row.count for row in answer.excluded}
    assert node_excluded == recount["excluded"]
    node_candidates = [
        (c.repository, c.merge_commit, c.contract_id) for c in answer.candidates
    ]
    assert node_candidates == recount["candidates"]
    assert [(c.repository, c.merge_commit) for c in answer.candidates] == sorted(
        (c[0], c[1]) for c in recount["candidates"]
    )


def test_every_task_is_well_formed_and_paths_are_hygiene_clean() -> None:
    tasks = _raw()["tasks"]
    merge_commits = [t["merge_commit"] for t in tasks]
    assert len(set(merge_commits)) == len(merge_commits)
    pairs = {(t["repository"], t["contract_id"]) for t in tasks}
    assert len(pairs) == len(tasks)
    for t in tasks:
        assert t["base_commit"] != t["merge_commit"]
        assert t["files_changed"] >= 1
        assert t["lines_changed"] >= 1
        for p in t["held_paths"]:
            assert not p.startswith("contracts/")
            assert not p.startswith(".github/")
        assert not set(t["check_files"]) & set(t["held_paths"])


def test_a_wrong_committed_band_count_is_caught() -> None:
    raw = _raw()
    recount_original = _recount(raw)
    answer_original = _handle(_request())
    node_band_original = {
        row.band.value: row.admitted for row in answer_original.by_band
    }
    assert node_band_original == recount_original["by_band"]
    mutated = copy.deepcopy(raw)
    tasks = mutated["tasks"]
    admitted = [t for t in tasks if t["control"] == "pass"]
    assert admitted
    victim = admitted[0]
    victim["band"] = (
        EnumSizeBand.M.value
        if victim["band"] == EnumSizeBand.S.value
        else EnumSizeBand.S.value
    )
    mutated_request = ModelCodeEvalSuiteCensusRequest.model_validate(mutated)
    answer_mutated = _handle(mutated_request)
    node_band_mutated = {row.band.value: row.admitted for row in answer_mutated.by_band}
    assert node_band_mutated != node_band_original


def test_flipping_a_passing_task_to_fail_check_drops_it_from_candidates_and_admitted() -> (
    None
):
    raw = _raw()
    answer_original = _handle(_request())
    original_candidates = [
        (c.repository, c.merge_commit, c.contract_id)
        for c in answer_original.candidates
    ]
    original_holds = answer_original.control.holds
    original_admitted = sum(row.admitted for row in answer_original.by_band)
    mutated = copy.deepcopy(raw)
    tasks = mutated["tasks"]
    passing = [t for t in tasks if t["control"] == "pass"]
    assert passing
    passing[0]["control"] = "fail_check"
    answer_mutated = _handle(ModelCodeEvalSuiteCensusRequest.model_validate(mutated))
    mutated_candidates = [
        (c.repository, c.merge_commit, c.contract_id) for c in answer_mutated.candidates
    ]
    assert len(mutated_candidates) == len(original_candidates) - 1
    assert answer_mutated.control.holds == original_holds - 1
    assert sum(row.admitted for row in answer_mutated.by_band) == original_admitted - 1
