# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: malformed requests end as typed failures, never a census."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.models.code_eval_suite.enum_suite_census_failure import (
    EnumSuiteCensusFailure,
)
from omnimarket.models.code_eval_suite.model_suite_census_request import (
    ModelCodeEvalSuiteCensusRequest,
)
from omnimarket.models.code_eval_suite.model_suite_census_result import (
    ModelCodeEvalSuiteCensus,
)
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.nodes.node_code_eval_suite_census_compute.handlers.handler_code_eval_suite_census import (
    HandlerCodeEvalSuiteCensus,
)
from tests.nodes.node_code_eval_suite_census_compute.support import (
    PASS,
    overlay,
    task,
)

pytestmark = pytest.mark.unit

S = EnumSizeBand.S
OVERLAY_ROOT = Path(__file__).resolve().parent / "fixtures" / "overlays"


def _handle(req: ModelCodeEvalSuiteCensusRequest) -> ModelCodeEvalSuiteCensus:
    return HandlerCodeEvalSuiteCensus(overlay=overlay()).handle(req)


def _assert_failure(
    answer: ModelCodeEvalSuiteCensus, code: EnumSuiteCensusFailure
) -> None:
    assert answer.failure is code
    assert answer.candidates == ()
    assert answer.by_band == ()
    assert answer.by_repository == ()
    assert answer.by_band_repository == ()
    assert answer.excluded == ()
    assert answer.control is None
    assert answer.detail


def test_error_chain_no_tasks_is_a_typed_failure() -> None:
    req = ModelCodeEvalSuiteCensusRequest(suite_id="s", tasks=())
    _assert_failure(_handle(req), EnumSuiteCensusFailure.NO_TASKS)


def test_error_chain_repeated_merge_commit_is_a_typed_failure() -> None:
    req = ModelCodeEvalSuiteCensusRequest(
        suite_id="s",
        tasks=(
            task("repo-a", 1, "OMN-1", S, PASS),
            task("repo-b", 1, "OMN-2", S, PASS),
        ),
    )
    answer = _handle(req)
    _assert_failure(answer, EnumSuiteCensusFailure.DUPLICATE_TASK)


def test_error_chain_repeated_repository_contract_is_a_typed_failure() -> None:
    req = ModelCodeEvalSuiteCensusRequest(
        suite_id="s",
        tasks=(
            task("repo-a", 1, "OMN-1", S, PASS),
            task("repo-a", 2, "OMN-1", S, PASS),
        ),
    )
    _assert_failure(_handle(req), EnumSuiteCensusFailure.DUPLICATE_TASK)


def test_error_chain_missing_overlay_is_a_typed_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    req = ModelCodeEvalSuiteCensusRequest(
        suite_id="s",
        tasks=(task("repo-a", 1, "OMN-1", S, PASS),),
    )
    _assert_failure(
        HandlerCodeEvalSuiteCensus().handle(req),
        EnumSuiteCensusFailure.OVERLAY_UNAVAILABLE,
    )


def test_error_chain_fixture_root_returns_a_census(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(OVERLAY_ROOT))
    req = ModelCodeEvalSuiteCensusRequest(
        suite_id="s",
        tasks=(task("repo-a", 1, "OMN-1", S, PASS),),
    )
    answer = HandlerCodeEvalSuiteCensus().handle(req)
    assert answer.failure is None
    assert answer.candidates_total == 1
