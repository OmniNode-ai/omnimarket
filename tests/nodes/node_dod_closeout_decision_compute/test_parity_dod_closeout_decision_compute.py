# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20675: the Python decisions reproduce the closer's JS pure block, case for case.

tests/fixtures/dod_closeout_decision_parity.json holds inputs and the outputs the JS pure
block of dod-closeout-sweep.js (plus the Accept-stage glue copied from it) produced for them
(source commit recorded in the file). This suite needs neither node nor the old script:
the fixture is the old behaviour.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_dod_closeout_decision_compute.handlers.handler_dod_closeout_decision import (
    HandlerDodCloseoutDecision,
)
from omnimarket.nodes.node_dod_closeout_decision_compute.models.model_dod_closeout_decision import (
    ModelDodCloseoutDecisionRequest as Request,
)

pytestmark = pytest.mark.unit

PARITY = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "fixtures/dod_closeout_decision_parity.json"
    ).read_text()
)
CASES = PARITY["cases"]
EXPECTED = PARITY["expected"]
HANDLER = HandlerDodCloseoutDecision()

_CAMEL_TO_SNAKE = {
    "fireId": "fire_id",
    "runKey": "run_key",
    "maxCandidates": "max_candidates",
    "chunkSize": "chunk_size",
    "gitOpTimeoutS": "git_op_timeout_s",
    "checkTimeoutS": "check_timeout_s",
    "noNewWork": "no_new_work",
    "returnBy": "return_by",
}


def _drop_none(value: Any) -> Any:
    """JSON.stringify omits undefined; the typed result carries None. Compare without both."""
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_none(v) for v in value]
    return value


def _snake(value: Any) -> Any:
    if isinstance(value, dict):
        return {_CAMEL_TO_SNAKE.get(k, k): _snake(v) for k, v in value.items()}
    return value


def _dump(model: Any) -> Any:
    return json.loads(model.model_dump_json())


def _request(kind: str, payload: dict[str, Any]) -> Request:
    return Request.model_validate({"kind": kind, **payload})


def _indices(kind: str) -> range:
    assert len(CASES[kind]) == len(EXPECTED[kind])
    return range(len(CASES[kind]))


def test_fixture_is_not_empty_and_has_both_outcomes_of_each_central_decision() -> None:
    assert sum(len(v) for v in CASES.values()) >= 700
    decisions = {e["ok"]["decision"] for e in EXPECTED["decide_ticket"] if "ok" in e}
    assert decisions == {"open", "done"}
    refusals = {e["ok"]["refusal"] for e in EXPECTED["refusal_of"]}
    assert None in refusals
    assert {
        "symbol-grep-only",
        "pr-exists-only",
        "inspection-only",
        "not-read-only",
    } <= (refusals)
    assert any("error" in e for e in EXPECTED["parse_args"])
    assert any("ok" in e for e in EXPECTED["parse_args"])


@pytest.mark.parametrize("n", _indices("parse_args"))
def test_parse_args_matches_the_old_block(n: int) -> None:
    expected = EXPECTED["parse_args"][n]
    request = _request("parse_args", CASES["parse_args"][n])
    if "error" in expected:
        with pytest.raises(ValueError, match=f"^{re.escape(expected['error'])}$"):
            HANDLER.handle(request)
        return
    result = HANDLER.handle(request)
    assert result.config is not None
    want = _snake(expected["ok"])
    # The config types project as a string; JS passes a numeric project through raw.
    want["project"] = str(want["project"])
    assert _dump(result.config) == want


@pytest.mark.parametrize("n", _indices("select_candidates"))
def test_candidate_selection_matches_the_old_block(n: int) -> None:
    result = HANDLER.handle(
        _request("select_candidates", CASES["select_candidates"][n])
    )
    assert result.selection is not None
    assert _dump(result.selection) == EXPECTED["select_candidates"][n]["ok"]


@pytest.mark.parametrize("n", _indices("chunk_list"))
def test_chunking_matches_the_old_block(n: int) -> None:
    result = HANDLER.handle(_request("chunk_list", CASES["chunk_list"][n]))
    assert result.chunks is not None
    assert [_dump(c) for c in result.chunks] == EXPECTED["chunk_list"][n]["ok"]


@pytest.mark.parametrize("n", _indices("refusal_of"))
def test_binding_refusal_matches_the_old_block(n: int) -> None:
    result = HANDLER.handle(_request("refusal_of", CASES["refusal_of"][n]))
    assert result.binding_refusal is not None
    assert _dump(result.binding_refusal) == EXPECTED["refusal_of"][n]["ok"]


@pytest.mark.parametrize("n", _indices("vet_bindings"))
def test_vetting_matches_the_old_block(n: int) -> None:
    result = HANDLER.handle(_request("vet_bindings", CASES["vet_bindings"][n]))
    expected = EXPECTED["vet_bindings"][n]["ok"]
    assert result.vetted is not None
    assert result.for_review is not None
    assert [_drop_none(v) for v in result.vetted] == _drop_none(expected["vetted"])
    assert [_drop_none(v) for v in result.for_review] == _drop_none(
        expected["for_review"]
    )


@pytest.mark.parametrize("n", _indices("decide_chunk"))
def test_chunk_decisions_match_the_old_block(n: int) -> None:
    result = HANDLER.handle(_request("decide_chunk", CASES["decide_chunk"][n]))
    expected = EXPECTED["decide_chunk"][n]["ok"]
    for field in ("merged", "decisions", "flips", "opens"):
        got = getattr(result, field)
        assert got is not None
        assert [_drop_none(v) for v in got] == _drop_none(expected[field]), field


@pytest.mark.parametrize("n", _indices("decide_ticket"))
def test_ticket_decision_matches_the_old_block(n: int) -> None:
    result = HANDLER.handle(_request("decide_ticket", CASES["decide_ticket"][n]))
    assert result.decision is not None
    assert _drop_none(_dump(result.decision)) == _drop_none(
        EXPECTED["decide_ticket"][n]["ok"]
    )


@pytest.mark.parametrize("n", _indices("open_comment"))
def test_open_comment_matches_the_old_block(n: int) -> None:
    result = HANDLER.handle(_request("open_comment", CASES["open_comment"][n]))
    assert result.open_comment is not None
    assert _dump(result.open_comment) == EXPECTED["open_comment"][n]["ok"]


@pytest.mark.parametrize("n", _indices("read_dod_verify"))
def test_verifier_receipt_reading_matches_the_old_block(n: int) -> None:
    result = HANDLER.handle(_request("read_dod_verify", CASES["read_dod_verify"][n]))
    assert result.dod_verify is not None
    assert _drop_none(_dump(result.dod_verify)) == _drop_none(
        EXPECTED["read_dod_verify"][n]["ok"]
    )


@pytest.mark.parametrize("n", _indices("merged_since"))
def test_merged_since_matches_the_old_block(n: int) -> None:
    result = HANDLER.handle(_request("merged_since", CASES["merged_since"][n]))
    assert result.merged_since == EXPECTED["merged_since"][n]["ok"]


@pytest.mark.parametrize("n", _indices("check_delegation"))
def test_delegation_check_matches_the_old_block(n: int) -> None:
    result = HANDLER.handle(_request("check_delegation", CASES["check_delegation"][n]))
    assert result.delegation_check is not None
    assert _dump(result.delegation_check) == EXPECTED["check_delegation"][n]["ok"]


def test_a_symbol_grep_is_never_a_binding_and_an_executing_test_is() -> None:
    """The 2026-09-30 failure: a grep bound to every criterion passed a ticket whose AC1 is false."""
    grep = {
        "label": "AC1",
        "kind": "command",
        "command": "git grep -n handle_credential src/",
        "cwd": "omnimarket",
        "ref": "origin/dev",
        "expect": "found",
    }
    test = {
        "label": "AC1",
        "kind": "test",
        "selector": "tests/unit/test_writer.py::test_rejects_postgres_only",
        "repo": "omnibase_infra",
        "ref": "origin/dev",
        "expect": "1 passed",
    }
    refused = HANDLER.handle(
        Request.model_validate({"kind": "refusal_of", "check": grep})
    )
    admitted = HANDLER.handle(
        Request.model_validate({"kind": "refusal_of", "check": test})
    )
    assert refused.binding_refusal is not None
    assert refused.binding_refusal.refusal == "symbol-grep-only"
    assert admitted.binding_refusal is not None
    assert admitted.binding_refusal.refusal is None
