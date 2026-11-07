# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of node_repo_state_query_effect over the bus (OMN-23033).

A merge event on the bus moves the host's view of the repository; a query published by the
caller is answered by the host's serve side on the completed terminal (golden), and a
query the mirror cannot answer comes back on the failed terminal (error). The in-memory
profile runs everywhere. The Redpanda profile runs the same chains against the broker
named in ``REPO_STATE_REDPANDA_BOOTSTRAP`` (a plaintext lab broker the caller started for
the proof, never the dev lane's):

    REPO_STATE_REDPANDA_BOOTSTRAP=<host:port> uv run pytest -m kafka tests/nodes/node_repo_state_query_effect -q
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.delegated_test_loop.lane_bus import open_lab_run_bus
from omnimarket.handlers.bus_binding.select import positional_opener
from omnimarket.nodes.node_canonical_clone_refresh_effect import (
    ModelRepoMerged,
    publish_repo_merged,
)
from omnimarket.nodes.node_repo_state_query_effect.handlers.handler_repo_state_query_serve import (
    RepoStateCaller,
    RepoStateHost,
    load_overlay,
    load_repo_state_topics,
)
from omnimarket.nodes.node_repo_state_query_effect.models.model_repo_state_query import (
    EnumRepoStateSource,
    EnumRepoStateWant,
    ModelRepoStateAnswer,
    ModelRepoStateQuery,
)
from tests.nodes.node_repo_state_query_effect.planted import HOST, REPO, plant

pytestmark = pytest.mark.unit
BOOTSTRAP_ENV = "REPO_STATE_REDPANDA_BOOTSTRAP"
_NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_repo_state_query_effect"
)


def test_the_contract_names_the_handler_topics_and_overlay() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    topics = load_repo_state_topics()
    assert topics.request == "onex.cmd.omnimarket.repo-state-query-requested.v1"
    assert topics.success == "onex.evt.omnimarket.repo-state-query-completed.v1"
    assert topics.failure == "onex.evt.omnimarket.repo-state-query-failed.v1"
    assert "onex.evt.omnimarket.repo-merged.v1" in topics.bus_state
    assert contract["handler"]["class"] == "HandlerRepoStateQuery"
    overlay = load_overlay(_NODE / "overlay.example.yaml")
    assert set(overlay.profiles) == {"inmemory", "redpanda"}
    assert overlay.profile(None).kind == "inmemory"
    assert overlay.profile("redpanda").kind == "kafka"


async def _chains(
    bus_args: tuple[str, str | None, str | None, None], tmp_path: Path
) -> None:
    p = plant(tmp_path)
    async with positional_opener(open_lab_run_bus)(*bus_args) as bus:
        host = RepoStateHost(bus, p.handler, p.bus_state)
        caller = RepoStateCaller(bus)
        await host.start()
        await caller.start()
        try:
            new = p.push_upstream()
            await publish_repo_merged(
                bus,
                ModelRepoMerged(
                    repo=REPO,
                    base="main",
                    merge_sha=new,
                    pr_number=9,
                    merged_at=datetime.now(UTC),
                    source="golden-chain",
                ),
            )
            for _ in range(200):  # the fold reads the merge off the bus
                if (head := p.bus_state.head(REPO, "main")) and head.sha == new:
                    break
                await asyncio.sleep(0.05)
            golden = await caller.query(
                ModelRepoStateQuery(
                    host=HOST,
                    repo=REPO,
                    want=EnumRepoStateWant.BRANCH_HEAD,
                    branch="main",
                ),
                timeout_seconds=30,
            )
            assert isinstance(golden, ModelRepoStateAnswer), golden
            assert golden.ok
            assert golden.source is EnumRepoStateSource.MIRROR_AFTER_PULL
            assert golden.mirror_sha == new
            assert p.runner.github_calls == 1

            error = await caller.query(
                ModelRepoStateQuery(
                    host=HOST, repo="OmniNode-ai/absent", want=EnumRepoStateWant.REFS
                ),
                timeout_seconds=30,
            )
            assert isinstance(error, ModelRepoStateAnswer), error
            assert not error.ok
            assert error.source is EnumRepoStateSource.UNAVAILABLE

            elsewhere = await caller.query(
                ModelRepoStateQuery(
                    host="h-other", repo=REPO, want=EnumRepoStateWant.REFS
                ),
                timeout_seconds=1,
            )
            assert elsewhere is None  # another host's query is never answered here
        finally:
            await caller.stop()
            await host.stop()


async def test_golden_and_error_chains_on_the_in_memory_bus(tmp_path: Path) -> None:
    await _chains(
        ("inmemory", None, None, None),
        tmp_path,
    )


@pytest.mark.kafka
@pytest.mark.integration
async def test_golden_and_error_chains_on_a_redpanda_bus(tmp_path: Path) -> None:
    bootstrap = os.environ.get(BOOTSTRAP_ENV)
    if not bootstrap:
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    await _chains(
        ("kafka", None, bootstrap, None),
        tmp_path,
    )
