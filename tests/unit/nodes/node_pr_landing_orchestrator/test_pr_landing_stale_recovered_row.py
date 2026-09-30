# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A row the runtime stale-recovered still decodes, and frees the PR (OMN-20119).

omnibase_infra's state_io ``recover_stale_rows`` gives up on a row whose effect
never answered: it sets the ``in_flight`` column to false, the ``state`` column
to FAILED, and writes ``failure_reason = "stale_in_flight_recovery"`` into the
payload JSON. ``ModelPrLandingWorkflowRow`` forbids extra keys, so before this
fix every later message for that PR failed to decode, was dead-lettered, and
the dead-letter replay put it back on the shared command topic, where the
autobind producer re-ran it too. On the .201 dev lane on 2026-09-30, 169 of 172
landing rows carried the annotation.

The payload below is the live row for omnibase_infra#4321, re-keyed to the
builders' PR.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_pr_arm_gate_compute.handlers.handler_arm_gate import (
    HandlerPrArmGate,
)
from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubMode,
    EnumPrLandingGithubOperation,
)
from omnimarket.nodes.node_pr_landing_orchestrator.handlers import (
    HandlerPrLandingOrchestrator,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.core import (
    PrLandingOrchestratorConfig,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.row_store import (
    InMemoryPrLandingRowStore,
    decode_row,
    encode_row,
)
from omnimarket.nodes.node_pr_landing_orchestrator.state_codec import StateIoCodec
from omnimarket.nodes.node_pr_landing_reducer.handlers.handler_pr_landing_reducer import (
    HandlerPrLandingReducer,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_head_check_verdict import (
    EnumHeadCheckVerdict,
)
from tests.unit.nodes.node_pr_landing_orchestrator._builders import (
    KEY,
    PR,
    REPO,
    FixedClassifier,
    only_request,
    prompt,
)

pytestmark = pytest.mark.unit


def _live_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "state": "UNSEEN",
        "landing": None,
        "base_ref": None,
        "in_flight": True,
        "pr_number": PR,
        "tenant_id": "platform",
        "check_runs": [],
        "dispatches": 1,
        "pr_node_id": None,
        "repository": REPO,
        "updated_at": "2026-09-29T22:10:35.165484Z",
        "landing_key": KEY,
        "pending_read": False,
        "reads_issued": 1,
        "pr_state_etag": None,
        "failure_reason": "stale_in_flight_recovery",
        "reads_answered": 0,
        "effect_in_flight": {
            "intent": {
                "kind": "github.read_pr_state",
                "detail": None,
                "head_sha": None,
                "pr_number": PR,
                "target_pr": None,
                "check_runs": [],
                "command_id": None,
                "repository": REPO,
                "agent_reason": None,
            },
            "read_id": 1,
            "sent_at": "2026-09-29T22:10:35.165484Z",
            "correlation_id": "a0efb050-e956-5e01-9e4c-513ec9569ffa",
        },
        "head_checks_etag": None,
        "agent_needed_sent": [],
        "bound_expired_for": None,
        "terminal_episodes": [],
        "head_checks_read_at": None,
    }
    row.update(overrides)
    return row


def test_stale_recovered_row_decodes_and_the_given_up_effect_is_closed() -> None:
    row = decode_row(json.dumps(_live_row()))
    assert row.landing_key == KEY
    # The runtime gave the effect up (in_flight column false): R4 must not wait on it.
    assert row.effect_in_flight is None
    # Everything else the row carried is kept.
    assert row.reads_issued == 1
    assert row.dispatches == 1


def test_stale_recovered_row_decodes_through_the_state_io_codec() -> None:
    row = StateIoCodec().decode(json.dumps(_live_row()).encode("utf-8"))
    assert row.effect_in_flight is None


def test_a_row_the_runtime_did_not_recover_keeps_its_effect_in_flight() -> None:
    payload = _live_row()
    del payload["failure_reason"]
    row = decode_row(json.dumps(payload))
    assert row.effect_in_flight is not None
    assert row.effect_in_flight.read_id == 1


def test_an_unknown_payload_key_is_still_refused() -> None:
    with pytest.raises(ValidationError):
        decode_row(json.dumps(_live_row(not_a_row_field="x")))


def test_re_encoding_a_recovered_row_drops_the_recovery_annotation() -> None:
    encoded = json.loads(encode_row(decode_row(json.dumps(_live_row()))))
    assert "failure_reason" not in encoded
    assert encoded["in_flight"] is False


async def test_stale_recovered_row_handles_a_prompt_with_a_fresh_read() -> None:
    store = InMemoryPrLandingRowStore()
    # Seed the durable row exactly as the runtime left it (version 3).
    store._rows[KEY] = (json.dumps(_live_row()), 3)
    handler = HandlerPrLandingOrchestrator(
        reducer=HandlerPrLandingReducer(),
        arm_gate=HandlerPrArmGate(),
        classifier=FixedClassifier(EnumHeadCheckVerdict.GREEN),
        config=PrLandingOrchestratorConfig(
            github_mode=EnumPrLandingGithubMode.ENFORCE,
            companion_exempt_repos=frozenset({REPO}),
        ),
        store=store,
    )
    read = only_request(await handler.handle(prompt()))
    assert read.operation is EnumPrLandingGithubOperation.READ_PR_STATE
    row, version = await store.load(KEY)
    assert version == 4
    assert row is not None
    assert row.reads_issued == 2
    assert row.effect_in_flight is not None
    assert row.effect_in_flight.read_id == 2
