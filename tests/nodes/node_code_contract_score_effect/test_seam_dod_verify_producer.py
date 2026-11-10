# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam of OMN-23626: the production producer of the dod_verify start command, and the scored event's join to the delegation run.

FIRST READ (OMN-23626): the start command for a delegated run names ``ticket_id`` and ``delegation_correlation_id`` and
nothing about a base commit, a draft diff or a diff hash; the verifier judges whatever tree it runs in. So the production
join today is the delegation run id and the gate verdict. This test pins both sides: the producer's start command carries
the run id the scored event joins on (and no draft or base field), and a completed delegation terminal's run id and gate
outcome are what the score request records as ``delegation_run_id`` and ``gate_accepted``. When the start command gains a
base commit, a draft or a diff hash this test fails, and the scorer is wired to read it from there.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_code_contract_score_effect.handlers.handler_code_contract_score import (
    HandlerCodeContractScore,
)
from omnimarket.nodes.node_code_contract_score_effect.models import (
    ModelCodeScored,
    ModelCodeScoreTry,
)
from omnimarket.nodes.node_delegation_dod_verify_request_compute.handlers.handler_delegation_dod_verify_request import (
    HandlerDelegationDodVerifyRequest,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from tests.nodes.node_code_contract_score_effect.fixture_repo import Fixture, request

pytestmark = pytest.mark.unit

DRAFT_FIELDS = {
    "base_commit",
    "draft_diff",
    "diff",
    "diff_sha256",
    "worktree",
    "base_sha",
    "draft",
}


def _terminal(run_id, accepted: bool) -> ModelDelegateSkillTerminalProjection:  # type: ignore[no-untyped-def]
    return ModelDelegateSkillTerminalProjection(
        status="completed",
        correlation_id=run_id,
        task_type="code_generation",
        model_name="the-served-model",
        prompt_text="Fix add.",
        response="diff",
        quality_gate_passed=accepted,
        quality_score=1.0,
        ticket_id="OMN-23626",
    )


def test_the_start_command_carries_the_join_key_and_no_draft_or_base() -> None:
    fields = set(ModelDodVerifyStartCommand.model_fields)
    assert {"ticket_id", "delegation_correlation_id"} <= fields
    assert not (fields & DRAFT_FIELDS), (
        "the producer now carries a draft or base: wire the scorer to read it"
    )


async def test_a_scored_event_joins_to_the_delegation_run_the_producer_named(
    fx: Fixture,
) -> None:
    run_id = uuid4()
    command = HandlerDelegationDodVerifyRequest().handle(
        _terminal(run_id, accepted=True)
    )
    assert command is not None
    base = request(fx, fx.good_diff)
    req = base.model_copy(
        update={
            "try_identity": ModelCodeScoreTry(
                task_key=command.ticket_id,
                delegation_run_id=command.delegation_correlation_id,
            ),
            "gate_accepted": True,
        }
    )
    result = await HandlerCodeContractScore().handle(req)
    assert result.scored is not None
    wire = ModelCodeScored.model_validate_json(result.scored.model_dump_json())
    assert wire.try_identity.delegation_run_id == run_id
    assert wire.gate_accepted is True


async def test_a_refused_delegation_is_still_scored_and_the_gate_stays_beside_the_score(
    fx: Fixture,
) -> None:
    run_id = uuid4()
    terminal = _terminal(run_id, accepted=False)
    req = request(fx, fx.good_diff).model_copy(
        update={
            "try_identity": ModelCodeScoreTry(
                task_key="OMN-23626", delegation_run_id=terminal.correlation_id
            ),
            "gate_accepted": terminal.quality_gate_passed,
        }
    )
    result = await HandlerCodeContractScore().handle(req)
    assert result.scored is not None
    assert result.scored.gate_accepted is False
    assert result.scored.verdict.checks.value == "pass"
