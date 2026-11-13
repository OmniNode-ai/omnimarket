# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A fake broker and reducer-record builders for the reap effect tests (OMN-23992)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import yaml

from omnimarket.nodes.contract_topics import contract_topic
from omnimarket.nodes.node_consumer_group_reap_effect.handlers.handler_group_reap import (
    HandlerGroupReap,
)
from omnimarket.nodes.node_consumer_group_reap_effect.models import (
    ModelDueGroup,
    ModelGroupReapCommand,
)
from omnimarket.nodes.node_consumer_group_reap_effect.overlay import (
    ModelGroupReapOverlay,
)
from omnimarket.nodes.node_consumer_group_state_reducer.handlers.handler_group_state import (
    fold,
)
from omnimarket.nodes.node_consumer_group_state_reducer.models import (
    ModelGroupCensusEvent,
    ModelGroupStateRecord,
    decision_digest,
)
from omnimarket.nodes.node_consumer_group_state_reducer.overlay import (
    ModelGroupStateOverlay,
)

REAP_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_consumer_group_reap_effect"
)
REDUCER_DIR = REAP_DIR.parent / "node_consumer_group_state_reducer"
LANE = "lane-a"
START = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
GROUP_A = "example.client.0123456789ab"
GROUP_B = "example.client.ba9876543210"
STABLE = "example.stable-group"
DRIFT = "example.client.host_drift_fleet.h053"
COMMAND_TOPIC = contract_topic(
    REAP_DIR / "contract.yaml", "subscribe_topics", "consumer-group-reap-requested.v1"
)
COMPLETED_TOPIC = contract_topic(
    REAP_DIR / "contract.yaml", "publish_topics", "consumer-group-reap-completed.v1"
)


def policy() -> ModelGroupStateOverlay:
    document = yaml.safe_load((REDUCER_DIR / "overlay.example.yaml").read_text())
    return ModelGroupStateOverlay.model_validate(document)


def execution(**cells: object) -> ModelGroupReapOverlay:
    document = yaml.safe_load((REAP_DIR / "overlay.example.yaml").read_text())
    document.update({"dry_run": False, "read_back_wait_seconds": 0, **cells})
    return ModelGroupReapOverlay.model_validate(document)


class FakeBroker:
    """Groups by name and state; a delete removes a name unless the test says it fails or sticks."""

    def __init__(self, groups: Mapping[str, str]) -> None:
        self.groups = dict(groups)
        self.fail: set[str] = set()
        self.stuck: set[str] = set()
        self.unhealthy = ""
        self.delete_calls: list[list[str]] = []

    def list(self, config: Mapping[str, str]) -> dict[str, str]:
        return dict(self.groups)

    def delete(self, config: Mapping[str, str], names: list[str]) -> dict[str, str]:
        self.delete_calls.append(list(names))
        outcome: dict[str, str] = {}
        for name in names:
            if name in self.fail:
                outcome[name] = "BROKER_ERROR"
                continue
            if name not in self.stuck:
                self.groups.pop(name, None)
            outcome[name] = ""
        return outcome

    def check(self, config: Mapping[str, str]) -> str:
        return self.unhealthy


def build_handler(
    broker: FakeBroker, overlay: ModelGroupReapOverlay | None = None
) -> HandlerGroupReap:
    return HandlerGroupReap(
        policy=policy(),
        overlay=overlay or execution(),
        lister=broker.list,
        deleter=broker.delete,
        health=broker.check,
        sleeper=lambda _seconds: None,
        resolve_broker=lambda _lane: ("broker", "environment", {}),
    )


def census(
    names: Mapping[str, str], seconds: int, correlation_id: UUID | None = None
) -> ModelGroupCensusEvent:
    return ModelGroupCensusEvent(
        correlation_id=correlation_id or uuid4(),
        bus_lane=LANE,
        observed_at=START + timedelta(seconds=seconds),
        groups=dict(names),
    )


def due_record(*names: str) -> ModelGroupStateRecord:
    """A reducer record, from two real folds, that names ``names`` as due."""
    groups = dict.fromkeys(names, "EMPTY") | {STABLE: "STABLE"}
    first = fold(None, census(groups, 0), policy())
    record = fold(
        first, census(groups, policy().empty_since_bound_seconds + 1), policy()
    )
    assert record.reap_due == tuple(sorted(names))
    return record


def command(record: ModelGroupStateRecord) -> ModelGroupReapCommand:
    return ModelGroupReapCommand.from_state_record(record, uuid4())


def forged(
    names: tuple[str, ...],
    *,
    empty_for_seconds: int,
    decided_by: str = "reducer",
) -> ModelGroupReapCommand:
    """A command built by hand with a digest that is correct for its own contents, so only the intended refusal fires."""
    observed = START + timedelta(seconds=empty_for_seconds)
    groups = tuple(ModelDueGroup(name=name, empty_since=START) for name in names)
    census_id = uuid4()
    return ModelGroupReapCommand(
        correlation_id=uuid4(),
        bus_lane=LANE,
        decided_by=decided_by,
        census_correlation_id=census_id,
        observed_at=observed,
        groups=groups,
        decision_digest=decision_digest(
            LANE, census_id, names, dict.fromkeys(names, START), observed
        ),
    )
