# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Integration proof for the GitHub PR poller effects runtime wiring.

OMN-11595 moves ``node_github_pr_poller_effect`` onto the effects runtime and
allows runtime-tick auto-wiring to invoke the handler without embedding poller
config in the tick payload. This test uses the real contract and handler, but
keeps the proof local: the contract default has no repos, so no GitHub or Kafka
I/O occurs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_infra.runtime.auto_wiring import (
    discover_contracts_from_paths,
    filter_manifest_for_runtime_profile,
)
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.nodes.node_github_pr_poller_effect.handlers.handler_github_api_poll import (
    HandlerGitHubApiPoll,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _runtime_tick() -> ModelRuntimeTick:
    now = datetime.now(tz=UTC)
    return ModelRuntimeTick(
        now=now,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid4(),
        scheduler_id="integration-omn-11595",
        tick_interval_ms=1000,
    )


async def test_github_pr_poller_contract_is_effects_owned_and_tick_invocable() -> None:
    contract_path = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_github_pr_poller_effect"
        / "contract.yaml"
    )

    manifest = discover_contracts_from_paths([contract_path])
    assert manifest.total_errors == 0
    assert manifest.total_discovered == 1

    effects_result = filter_manifest_for_runtime_profile(manifest, "effects")
    main_result = filter_manifest_for_runtime_profile(manifest, "main")
    assert [contract.name for contract in effects_result.manifest.contracts] == [
        "node_github_pr_poller_effect"
    ]
    assert main_result.manifest.contracts == ()

    handler = HandlerGitHubApiPoll()
    result = await handler.handle(_runtime_tick())

    assert result.errors == []
    assert result.repos_polled == []
    assert result.prs_polled == 0
    assert result.pending_events == []
