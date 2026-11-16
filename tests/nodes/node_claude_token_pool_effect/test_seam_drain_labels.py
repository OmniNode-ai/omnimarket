# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam between the usage verdict node's result and the pool's resolve request: ``drain_labels`` (OMN-23636).

Producer side: ``node_pool_usage_verdict_compute`` answers ``ModelPoolUsageResult`` with
``drain_labels: tuple[str, ...]`` and its contract's ``seams.pool_resolve`` names this node's
``resolve`` and that field. Consumer side: the lane start reads the field from the verdict's result
(``ModelPoolDrainAnswer``) and hands it to ``ModelClaudeTokenPoolRequest.drain_labels``.

The verdict's result crosses the in-memory event bus as JSON and the real pool handler resolves on
what arrives. The as-shipped result must steer the draw off the drained label; a planted rename of
the field and a planted change of its element type are the failing controls, and each must be refused,
never read as "nothing is draining". The producer's shape is pinned here as recorded from its contract;
where the verdict node is installed beside this one, the live model and contract are held to the pin.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import typing
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import SecretStr, ValidationError

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    HandlerClaudeTokenPool,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_pool_steering import (
    ModelPoolDrainAnswer,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolRequest,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_bus import (
    InmemoryBus,
)

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_claude_token_pool_effect"
)
PEER = "omnimarket.nodes.node_pool_usage_verdict_compute"
#: The seam's field and element type, on both sides.
FIELD = "drain_labels"
ELEMENT = str
#: The verdict node's result as its contract and model declare it (recorded 2026-11-10 from the
#: verdict node's change): every output key, with one draining label.
RECORDED_RESULT: dict[str, Any] = {
    "labels": [],
    "drain_labels": ["acct-a"],
    "ok": 1,
    "drain": 1,
    "limited": 0,
    "unknown": 0,
    "correlation_id": "6e375228-96c7-400a-a728-e8950a8144e9",
}


def fake_token(seed: str) -> str:
    return "sk-ant-" + "oat01-" + (seed * 64)[:96]


def tuple_element(annotation: object) -> object | None:
    args = typing.get_args(annotation)
    if typing.get_origin(annotation) is not tuple or len(args) != 2:
        return None
    return args[0] if args[1] is Ellipsis else None


def test_both_models_of_this_side_carry_the_field_as_a_tuple_of_labels() -> None:
    request = ModelClaudeTokenPoolRequest.model_fields[FIELD]
    answer = ModelPoolDrainAnswer.model_fields[FIELD]
    assert tuple_element(request.annotation) is ELEMENT
    assert tuple_element(answer.annotation) is ELEMENT
    # The answer must fail on a result that lacks the field; a default would read a rename as "none".
    assert answer.is_required()


def test_the_contract_names_the_seam_and_the_recorded_result_carries_it() -> None:
    seam = yaml.safe_load((NODE / "contract.yaml").read_text("utf-8"))["seams"][
        "usage_verdict"
    ]
    assert seam == {
        "node": "node_pool_usage_verdict_compute",
        "operation": "resolve",
        "field": FIELD,
        "element_type": ELEMENT.__name__,
    }
    assert FIELD in RECORDED_RESULT


def test_the_verdict_node_where_installed_agrees_with_the_pin() -> None:
    """Runs against the live producer once both changes are on one tree; until then the pin stands alone."""
    if importlib.util.find_spec(PEER) is None:
        pytest.skip("the usage verdict node is not on this tree yet")
    result_model = importlib.import_module(
        f"{PEER}.models.model_pool_usage_result"
    ).ModelPoolUsageResult
    assert tuple_element(result_model.model_fields[FIELD].annotation) is ELEMENT
    assert set(result_model.model_fields) == set(RECORDED_RESULT)
    result_model.model_validate(RECORDED_RESULT)
    peer_dir = Path(importlib.import_module(PEER).__file__ or "").parent
    seam = yaml.safe_load((peer_dir / "contract.yaml").read_text("utf-8"))["seams"][
        "pool_resolve"
    ]
    assert (seam["node"], seam["operation"], seam["field"]) == (
        "node_claude_token_pool_effect",
        "resolve",
        FIELD,
    )


def seam_cases() -> list[tuple[str, dict[str, Any]]]:
    renamed = {
        ("drained_labels" if key == FIELD else key): value
        for key, value in RECORDED_RESULT.items()
    }
    retyped = {**RECORDED_RESULT, FIELD: [{"label": "acct-a"}]}
    return [
        ("as-shipped", RECORDED_RESULT),
        ("planted-rename", renamed),
        ("planted-element-type", retyped),
    ]


def consumer(tmp: Path) -> Any:
    """The consumer side: the real pool handler resolving on the verdict result that arrived."""
    handler = HandlerClaudeTokenPool(
        store=LocalByokCredentialStore(db_path=tmp / "store" / "evidence.sqlite"),
        status_path=tmp / "cache" / "claude-token-pool.json",
        host="h-test",
    )
    for label in ("acct-a", "acct-b"):
        handler.handle(
            ModelClaudeTokenPoolRequest(
                operation="add",
                label=label,
                setup_token_output=SecretStr(fake_token(label[-1])),
            )
        )

    def decide(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            answer = ModelPoolDrainAnswer.model_validate(payload["result"])
            request = ModelClaudeTokenPoolRequest.model_validate(
                {"operation": "resolve", FIELD: answer.model_dump()[FIELD]}
            )
        except ValidationError as refused:
            return {
                "id": payload["id"],
                "refused": sorted(str(e["loc"][0]) for e in refused.errors()),
            }
        # The pool handler runs its own event loop for the secret store, so it resolves on a worker
        # thread while the bus's loop waits for the reply.
        with ThreadPoolExecutor(max_workers=1) as worker:
            drawn = worker.submit(handler.handle, request).result()
        return {"id": payload["id"], "chosen": drawn.chosen}

    return decide


def test_the_verdict_result_steers_the_draw_across_the_in_memory_bus(
    tmp_path: Path,
) -> None:
    decide = consumer(tmp_path)

    async def run() -> dict[str, dict[str, Any]]:
        bus = InmemoryBus(decide, "pool-drain-labels-seam")
        await bus.start()
        try:
            return {
                name: await bus.call({"id": str(uuid.uuid4()), "result": result})
                for name, result in seam_cases()
            }
        finally:
            await bus.stop()

    replies = asyncio.run(run())
    # The drained label is first in the pool and the draw lands on the other one.
    assert replies["as-shipped"]["chosen"] == "acct-b"
    assert replies["planted-rename"]["refused"] == [FIELD]
    assert replies["planted-element-type"]["refused"] == [FIELD]


def test_the_pool_request_refuses_the_field_under_another_name() -> None:
    with pytest.raises(ValidationError):
        ModelClaudeTokenPoolRequest.model_validate(
            {"operation": "resolve", "drained_labels": ["acct-a"]}
        )
    with pytest.raises(ValidationError):
        ModelClaudeTokenPoolRequest.model_validate(
            {"operation": "resolve", FIELD: [{"label": "acct-a"}]}
        )
