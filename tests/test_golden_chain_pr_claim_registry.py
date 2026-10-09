# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for the PR claim registry effect."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_pr_claim_registry_effect.handlers import (
    HandlerPrClaimRegistry,
)
from omnimarket.nodes.node_pr_claim_registry_effect.models import (
    EnumPrClaimOperation,
    ModelPrClaimRegistryRequest,
    ModelPrClaimRegistryResult,
)

pytestmark = pytest.mark.unit

PR = "omninode-ai/omniclaude#247"
NOW = "2026-10-09T12:00:00Z"


def command(
    tmp_path: Path, operation: EnumPrClaimOperation, **fields: Any
) -> ModelPrClaimRegistryRequest:
    return ModelPrClaimRegistryRequest(
        operation=operation,
        claims_dir=str(tmp_path / "claims"),
        instance_id_path=str(tmp_path / "instance_id"),
        now=NOW,
        host="host-a",
        **fields,
    )


def test_golden_chain_acquire_heartbeat_release(tmp_path: Path) -> None:
    handler = HandlerPrClaimRegistry()
    acquired = handler.handle(
        command(
            tmp_path,
            EnumPrClaimOperation.ACQUIRE,
            pr_key=PR,
            run_id="R1",
            action="merge",
            lane_id="laneA",
        )
    )
    assert isinstance(acquired, ModelPrClaimRegistryResult)
    assert acquired.succeeded
    path = tmp_path / "claims" / "omninode-ai--omniclaude--247.json"
    assert json.loads(path.read_text())["lane_id"] == "laneA"

    peer = handler.handle(
        command(
            tmp_path,
            EnumPrClaimOperation.ACQUIRE,
            pr_key=PR,
            run_id="R2",
            action="merge",
            lane_id="laneB",
        )
    )
    assert not peer.succeeded
    assert "actively claimed by run R1" in peer.messages[0]

    held = handler.handle(command(tmp_path, EnumPrClaimOperation.GET_CLAIM, pr_key=PR))
    assert held.claim is not None
    assert held.claim.claimed_by_run == "R1"
    assert handler.handle(
        command(tmp_path, EnumPrClaimOperation.HAS_ACTIVE, pr_key=PR)
    ).succeeded

    released = handler.handle(
        command(
            tmp_path,
            EnumPrClaimOperation.RELEASE,
            pr_key=PR,
            run_id="R1",
            lane_id="laneA",
        )
    )
    assert released.succeeded
    assert not path.exists()


def test_error_chain_malformed_command_never_reaches_the_handler() -> None:
    with pytest.raises(ValidationError) as caught:
        ModelPrClaimRegistryRequest.model_validate({"operation": "release"})
    missing = {err["loc"][0] for err in caught.value.errors()}
    assert {"claims_dir", "now"} <= missing


def test_error_chain_an_unreadable_claim_is_reported_not_raised(tmp_path: Path) -> None:
    claims = tmp_path / "claims"
    claims.mkdir()
    (claims / "omninode-ai--omniclaude--247.json").write_text("{broken")
    result = HandlerPrClaimRegistry().handle(
        command(tmp_path, EnumPrClaimOperation.GET_CLAIM, pr_key=PR)
    )
    assert result.claim is None
    assert not result.succeeded
