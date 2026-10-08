# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ingress observation -> real handler -> check -> terminal event payload."""

import pytest
import yaml

from omnimarket.nodes.node_branch_claim_check_effect.models import (
    ModelBranchClaimCheckResult,
)
from tests.test_omn20708_branch_claim_check_handler import (
    Commits,
    Poster,
    Reader,
    Witness,
    handler,
    observation,
)
from tests.test_omn20708_branch_claim_resolution import CONTRACT, commit, db_rows, row

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("database_error", [False, True])
def test_golden_chain(database_error):
    claim = row()
    reader = Reader(
        db_rows([claim]), OSError("database offline") if database_error else None
    )
    poster = Poster()
    node = handler(reader, Witness([claim]), Commits([commit()]), poster)
    ingress = observation()
    result = node.handle(ingress)
    assert len(poster.posts) == 1
    post = poster.posts[0]
    contract = yaml.safe_load(CONTRACT.read_text())
    assert post["name"] == contract["branch_claim"]["check_name"]
    assert post["head_sha"] == ingress.head_sha
    assert node.terminal_event == contract["terminal_event"]
    assert node.terminal_event == "onex.evt.omnimarket.branch-claim-check-completed.v1"
    assert node.terminal_event in contract["event_bus"]["publish_topics"]
    assert (
        ModelBranchClaimCheckResult.model_validate_json(result.model_dump_json())
        == result
    )
    expected = "did-not-run" if database_error else "held-by-pusher"
    assert (
        post["summary"].splitlines()[0].startswith(f"branch-claim-outcome: {expected} ")
    )
    assert post["conclusion"] == ("failure" if database_error else "success")
    assert node.handle(ingress).correlation_id == result.correlation_id


@pytest.mark.parametrize("route_number", [0, 1])
def test_real_runtime_typed_dispatch(route_number):
    import asyncio

    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.auto_wiring.handler_wiring import (
        _make_dispatch_callback,
        _typed_def_b_input_model,
    )
    from pydantic import BaseModel

    from tests.test_omn20708_branch_claim_check_handler import request

    poster = Poster()
    node = handler(
        Reader(db_rows([row()])), Witness([row()]), Commits([commit()]), poster
    )
    manifest = discover_contracts_from_paths([CONTRACT])
    assert not manifest.errors
    contract = manifest.contracts[0]
    route = contract.handler_routing.handlers[route_number]
    assert _typed_def_b_input_model(node) is BaseModel
    payload = observation() if route_number == 0 else request()
    callback = _make_dispatch_callback(node, event_model=route.event_model)
    dispatch = asyncio.run(
        callback(
            {
                "payload": payload.model_dump(mode="json"),
                "correlation_id": "00000000-0000-0000-0000-000000000001",
            }
        )
    )
    assert len(poster.posts) == 1
    assert dispatch is not None
    terminal = dispatch.output_events[0]
    assert (
        ModelBranchClaimCheckResult.model_validate(terminal).outcome.value
        == "held-by-pusher"
    )
