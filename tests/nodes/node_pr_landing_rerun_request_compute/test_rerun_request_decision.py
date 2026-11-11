# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The rerun request decision node: registered, contract-bound, and each verdict decided on its own (OMN-23830)."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import get_type_hints
from uuid import uuid4

import pytest
import yaml

from omnimarket.events.pr_landing_rerun_request.enum_rerun_verdict import (
    EnumRerunVerdict,
)
from omnimarket.events.pr_landing_rerun_request.model_landing_rerun_answer import (
    ModelLandingRerunAnswer,
)
from omnimarket.events.pr_landing_rerun_request.model_landing_rerun_request import (
    ModelLandingRerunRequest,
)
from omnimarket.nodes.node_pr_landing_rerun_request_compute import (
    NodePrLandingRerunRequestCompute,
)
from omnimarket.nodes.node_pr_landing_rerun_request_compute.handlers.handler_pr_landing_rerun_request import (
    HandlerPrLandingRerunRequest,
)
from omnimarket.nodes.node_pr_landing_rerun_request_compute.models.model_rerun_decide_input import (
    ModelRerunDecideInput,
    ModelRerunGrant,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    ModelTickSnapshot,
)

pytestmark = pytest.mark.unit

NODE = "node_pr_landing_rerun_request_compute"
REPO = Path(__file__).resolve().parents[3]
SNAP = ModelTickSnapshot.model_validate_json(
    (REPO / "tests/fixtures/pr_landing_tick_replay/tick_725_omnimarket.json").read_text(
        encoding="utf-8"
    )
)
CURRENT = next(p for p in SNAP.facts.prs if p.pr == "OmniNode-ai/omnimarket#3183")
JOB = CURRENT.red_checks[0]


def _input(
    *,
    job: str = JOB,
    head: str | None = None,
    granted=(),
    limit: int = 1,
    current=CURRENT,
) -> ModelRerunDecideInput:
    return ModelRerunDecideInput(
        request=ModelLandingRerunRequest(
            correlation_id=uuid4(),
            pr=CURRENT.pr,
            head_sha=head or CURRENT.head_sha,
            job=job,
            reason="runner outage",
            requester="lane-a",
        ),
        current=current,
        granted=granted,
        max_reruns_per_head=limit,
    )


def test_node_is_registered_and_its_contract_binds_the_handler() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert (
        pyproject["project"]["entry-points"]["onex.nodes"][NODE]
        == f"omnimarket.nodes.{NODE}"
    )
    root = REPO / "src" / "omnimarket" / "nodes" / NODE
    contract = yaml.safe_load((root / "contract.yaml").read_text())
    assert (
        contract["name"]
        == yaml.safe_load((root / "metadata.yaml").read_text())["name"]
        == NODE
    )
    assert contract["node_type"] == "COMPUTE_GENERIC"
    hints = get_type_hints(NodePrLandingRerunRequestCompute.handle)
    assert hints["request"] is ModelRerunDecideInput
    assert hints["return"] is ModelLandingRerunAnswer


def test_golden_a_failed_job_on_the_current_head_is_rerun() -> None:
    answer = HandlerPrLandingRerunRequest().handle(_input())
    assert answer.verdict is EnumRerunVerdict.RERUN
    assert (answer.reruns_on_head, answer.limit) == (1, 1)
    assert (answer.requester, answer.reason) == ("lane-a", "runner outage")


def test_each_refusal_is_its_own_verdict() -> None:
    decide = HandlerPrLandingRerunRequest().handle
    on_head = ModelRerunGrant(head_sha=CURRENT.head_sha, job=JOB)
    assert decide(_input(granted=(on_head,))).verdict is EnumRerunVerdict.DUPLICATE
    other = ModelRerunGrant(head_sha=CURRENT.head_sha, job="some other job")
    assert decide(_input(granted=(other,))).verdict is EnumRerunVerdict.LIMIT_REACHED
    assert decide(_input(head="0" * 40)).verdict is EnumRerunVerdict.HEAD_MOVED
    assert decide(_input(current=None)).verdict is EnumRerunVerdict.HEAD_MOVED
    assert decide(_input(job="green")).verdict is EnumRerunVerdict.JOB_NOT_FAILED
    # a grant on another head counts for nothing on this one
    stale = ModelRerunGrant(head_sha="1" * 40, job=JOB)
    assert decide(_input(granted=(stale,))).verdict is EnumRerunVerdict.RERUN


def test_the_same_input_gives_the_same_answer() -> None:
    request = _input()
    decide = HandlerPrLandingRerunRequest().handle
    assert decide(request) == decide(request)
