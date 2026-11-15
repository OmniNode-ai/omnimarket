# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24367 AC1 -- the announcement carries what the host is, and its facts digest moves with the facts and not with load."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.nodes.node_host_introspection_compute.handlers import (
    HandlerHostIntrospection,
    facts_digest,
)
from omnimarket.nodes.node_host_introspection_compute.models import (
    EnumAnnounceReason,
    ModelHostAvailability,
    ModelHostFacts,
    ModelHostGpu,
    ModelHostIntrospectionRequest,
    ModelHostLaneAccount,
    ModelHostRuntime,
    ModelHostServedModel,
    ModelHostSoftware,
)

pytestmark = pytest.mark.unit

AT = datetime(2026, 11, 15, 0, 0, tzinfo=UTC)


def _facts(**changes: object) -> ModelHostFacts:
    base = ModelHostFacts(
        host_name="h201",
        roles=["lane", "model"],
        cores=32,
        mem_total_bytes=128 * 1024**3,
        gpus=[ModelHostGpu(name="RTX 6000", memory_total_mib=49152)],
        models_served=[ModelHostServedModel(name="qwen3.8-27b")],
        runtimes=[
            ModelHostRuntime(name="docker", version="27.3.1"),
            ModelHostRuntime(name="python3", version="3.12.4"),
        ],
        software=[
            ModelHostSoftware(name="onex", version="1.4.0", commit="abc1234"),
            ModelHostSoftware(name="uv", version="0.5.1"),
        ],
        lane_accounts=[ModelHostLaneAccount(name="jonah", signed_in=True)],
        availability=ModelHostAvailability(takes_work=True),
    )
    return base.model_copy(update=changes)


def _capacity(load1: float = 1.0) -> ModelHostCapacityAdvertisement:
    return ModelHostCapacityAdvertisement(
        host_name="h201",
        cores=32,
        load1=load1,
        mem_available_bytes=64 * 1024**3,
        tools=["git", "uv"],
        advertised_at=AT,
        cadence_seconds=10,
        mem_total_bytes=128 * 1024**3,
    )


def _announce(facts: ModelHostFacts, capacity: ModelHostCapacityAdvertisement):
    return HandlerHostIntrospection().handle(
        ModelHostIntrospectionRequest(
            facts=facts,
            capacity=capacity,
            reason=EnumAnnounceReason.START,
            announced_at=AT,
        )
    )


def test_announcement_carries_every_kind_of_fact() -> None:
    announcement = _announce(_facts(), _capacity())
    dumped = announcement.model_dump(mode="json")
    assert dumped["host_name"] == "h201"
    assert dumped["facts"]["roles"] == ["lane", "model"]
    assert dumped["facts"]["gpus"][0]["name"] == "RTX 6000"
    assert dumped["facts"]["models_served"][0]["name"] == "qwen3.8-27b"
    assert {r["name"] for r in dumped["facts"]["runtimes"]} == {"docker", "python3"}
    onex = next(s for s in dumped["facts"]["software"] if s["name"] == "onex")
    assert (onex["version"], onex["commit"]) == ("1.4.0", "abc1234")
    assert dumped["facts"]["lane_accounts"][0]["signed_in"] is True
    assert dumped["facts"]["availability"]["takes_work"] is True
    assert dumped["capacity"]["cores"] == 32
    assert len(dumped["facts_digest"]) == 64


def test_digest_ignores_load_and_every_capacity_reading() -> None:
    quiet = _announce(_facts(), _capacity(load1=0.1))
    busy = _announce(
        _facts(), _capacity(load1=31.0).model_copy(update={"running_units": 4})
    )
    assert quiet.facts_digest == busy.facts_digest


def test_digest_ignores_reading_order() -> None:
    reordered = _facts(
        runtimes=list(reversed(_facts().runtimes)), roles=["model", "lane"]
    )
    assert facts_digest(reordered) == facts_digest(_facts())


@pytest.mark.parametrize(
    "changed",
    [
        pytest.param(
            {
                "software": [
                    ModelHostSoftware(name="onex", version="1.5.0", commit="abc1234"),
                    ModelHostSoftware(name="uv", version="0.5.1"),
                ]
            },
            id="version",
        ),
        pytest.param(
            {
                "software": [
                    ModelHostSoftware(name="onex", version="1.4.0", commit="def5678"),
                    ModelHostSoftware(name="uv", version="0.5.1"),
                ]
            },
            id="commit",
        ),
        pytest.param(
            {
                "runtimes": [
                    ModelHostRuntime(name="docker", version="27.4.0"),
                    ModelHostRuntime(name="python3", version="3.12.4"),
                ]
            },
            id="runtime",
        ),
        pytest.param({"gpus": []}, id="gpus"),
        pytest.param({"models_served": []}, id="models"),
        pytest.param({"roles": ["lane"]}, id="roles"),
        pytest.param(
            {"lane_accounts": [ModelHostLaneAccount(name="jonah", signed_in=False)]},
            id="accounts",
        ),
        pytest.param(
            {"availability": ModelHostAvailability(takes_work=False, reason="repair")},
            id="availability",
        ),
    ],
)
def test_digest_moves_with_each_kind_of_fact(changed: dict[str, object]) -> None:
    assert facts_digest(_facts(**changed)) != facts_digest(_facts())
