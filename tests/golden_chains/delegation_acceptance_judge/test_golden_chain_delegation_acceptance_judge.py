# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for node_delegation_acceptance_judge_compute (OMN-20429).

Each chain is a JSON fixture: one typed request on the node's command topic and the
result it must produce, replayed through the handler that the node's contract wires.

  * POSITIVE: the 72 recorded primary verdicts and 8 second-judge verdicts of the
    2026-10-08 rubric v1 run, scored again, give the published matrix (accepted
    counts, Wilson intervals, mean quality, failure counts) and agreement exactly.
    The item texts are withheld; scoring reads only ids, cells and verdicts.
  * NEGATIVE (score): the same run with every second-judge verdict inverted fails
    the kappa gate.
  * NEGATIVE (check): a judge reply with a duplicated, missing and unknown id, an
    accept below quality 2, a reject with failure class none and a failure class
    outside the vocabulary is refused, one issue per defect.
"""

from __future__ import annotations

import importlib
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.contracts.contract_loader import load_contract

from omnimarket.models.delegation_acceptance_judge.model_acceptance_judge_request import (
    ModelAcceptanceJudgeRequest,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.handlers.handler_delegation_acceptance_judge import (
    HandlerDelegationAcceptanceJudge,
)

_CHAIN_DIR = Path(__file__).resolve().parent
_NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_delegation_acceptance_judge_compute"
)
_COMMAND_TOPIC = "onex.cmd.omnimarket.delegation-acceptance-judge-requested.v1"
_TERMINAL_TOPIC = "onex.evt.omnimarket.delegation-acceptance-judged.v1"
_CHAIN_FILES = sorted(_CHAIN_DIR.glob("chain_*.json"))
_RUBRIC_YAML = (
    files("omnimarket")
    .joinpath("configs/delegation_acceptance_judge_rubric.v1.yaml")
    .read_text(encoding="utf-8")
)


def _load_chain(path: Path) -> dict[str, Any]:
    chain: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return chain


def _resolve_wired_handler() -> type[HandlerDelegationAcceptanceJudge]:
    """Resolve the handler through the contract the runtime loads."""
    contract = load_contract(_NODE_DIR / "contract.yaml")
    event_bus = contract["event_bus"]
    assert isinstance(event_bus, dict)
    assert _COMMAND_TOPIC in event_bus["subscribe_topics"]
    assert _TERMINAL_TOPIC in event_bus["publish_topics"]
    routing = contract["handler_routing"]
    assert isinstance(routing, dict)
    handler_ref = routing["handlers"][0]["handler"]
    resolved = getattr(
        importlib.import_module(handler_ref["module"]), handler_ref["name"]
    )
    assert resolved is HandlerDelegationAcceptanceJudge
    return HandlerDelegationAcceptanceJudge


def _replay(chain: dict[str, Any]) -> dict[str, Any]:
    request = ModelAcceptanceJudgeRequest.model_validate(
        {"rubric_yaml": _RUBRIC_YAML, **chain["request"]}
    )
    result = _resolve_wired_handler()().handle(request)
    replayed: dict[str, Any] = json.loads(result.model_dump_json())
    return replayed


@pytest.mark.unit
@pytest.mark.parametrize("chain_path", _CHAIN_FILES, ids=[p.stem for p in _CHAIN_FILES])
class TestDelegationAcceptanceJudgeGoldenChains:
    def test_replay_matches_the_recorded_result(self, chain_path: Path) -> None:
        chain = _load_chain(chain_path)
        replayed = _replay(chain)
        for field, expected in chain["expected"].items():
            if field == "agreement":
                assert {k: replayed[field][k] for k in expected} == expected
            elif field == "issues":
                assert [
                    {"code": i["code"], "item_id": i["item_id"]}
                    for i in replayed[field]
                ] == expected
            else:
                assert replayed[field] == expected, f"{chain['chain']}: {field} drifted"

    def test_replay_is_deterministic(self, chain_path: Path) -> None:
        chain = _load_chain(chain_path)
        assert _replay(chain) == _replay(chain)


def test_the_chains_cover_a_golden_and_both_error_paths() -> None:
    names = {_load_chain(p)["chain"] for p in _CHAIN_FILES}
    assert names == {
        "positive_recorded_run_reproduces_matrix",
        "negative_second_judge_disagrees",
        "negative_refused_judge_reply",
    }
