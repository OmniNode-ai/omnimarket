# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20730: a red landing-branch head asks for exactly one cause lane, unless one owns the cause.

The owner of a dev-head cause is, in order: a cause lane this process asked for inside its lease,
the bus path's durable owner claim (the PR path's dev_head owner key, pr_lifecycle_ledger_entries),
or a live cause CLAIM on the work ledger (a remote lane's ``cause=<repo>:<check>`` or the landing
controller's relay CLAIM ``cause=<cause key> | check=<checks>``). An unreadable owner withholds the
request. Dispatch-once across a restart rests on the receiver deduplicating ``dispatch_key``
(formal/ci_red_triage, OneStartPerIdentity with ReceiverDedup).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import _topics_for_handler_entry

from omnimarket.events.topics import (
    DEV_CAUSE_LANE_REQUESTED_TOPIC_V1,
    DEV_HEAD_RED_DECIDED_TOPIC_V1,
    DEV_HEAD_RED_TOPIC_V1,
)
from omnimarket.models.ci_red_triage import ci_red_cause_key
from omnimarket.models.dev_head_red import (
    EnumDevHeadRedAction,
    ModelDevCauseLaneRequest,
    ModelDevHeadRedDecided,
    ModelDevHeadRedEvent,
    dev_cause_dispatch_key,
    dev_head_owner_key,
    dev_head_red_event_id,
    live_cause_owner,
)
from omnimarket.models.work_ledger_query import ModelWorkLedgerRowRecord
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.ci_red_claims import (
    CiRedClaimsUnreadError,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.dev_cause_owners import (
    DevCauseOwnersUnreadError,
    UnboundDevCauseOwners,
    dev_cause_owners_from_contract,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_dev_head_red_dispatch import (
    HandlerDevHeadRedDispatch,
)
from omnimarket.nodes.node_pr_state_emit_effect.handlers.handler_detect_dev_head_red import (
    DEV_HEAD_RED_INDEX,
    HandlerDetectDevHeadRed,
)

pytestmark = pytest.mark.unit

REPO = "OmniNode-ai/omnimarket"
CHECK = "CI Summary"
SHA = "a" * 40
NOW = datetime(2026, 10, 13, 21, 43, tzinfo=UTC)
LIVE = timedelta(seconds=5400)
CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_pr_lifecycle_orchestrator/contract.yaml"
)


@pytest.fixture(autouse=True)
def empty_index() -> Iterator[None]:
    DEV_HEAD_RED_INDEX.clear()
    yield
    DEV_HEAD_RED_INDEX.clear()


def red(sha: str = SHA, check: str = CHECK) -> ModelDevHeadRedEvent:
    return ModelDevHeadRedEvent(
        event_id=dev_head_red_event_id(REPO, "dev", sha, check),
        repo=REPO,
        branch="dev",
        head_sha=sha,
        check=check,
        conclusion="failure",
        completed_at="2026-10-13T21:42:00Z",
        source_delivery="6f1c2d3e-0000-4000-8000-000000000001",
    )


def row(ts: datetime, kind: str, lane: str, *cells: str) -> ModelWorkLedgerRowRecord:
    text = " | ".join(
        [ts.strftime("%Y-%m-%dT%H:%M:%SZ"), kind, f"lane={lane}", *cells, "text"]
    )
    return ModelWorkLedgerRowRecord(
        row_id=f"{lane}-{kind}-{ts.isoformat()}",
        row_ts=ts,
        row_type=kind,
        row_lane=lane,
        text=text,
        raw_row=text,
        source="markdown",
    )


class FakeBusClaims:
    def __init__(self, owned: Iterable[str] = (), *, unread: bool = False) -> None:
        self.owned_keys = set(owned)
        self.unread = unread

    def decided(self, decision_key: str) -> bool:
        return False

    def owned(self, owner_key: str) -> bool:
        if self.unread:
            raise CiRedClaimsUnreadError("projection down")
        return owner_key in self.owned_keys

    def absorbing_cause(self, pr_number: int, cause_keys: Iterable[str]) -> str | None:
        return None


class FakeLedgerOwners:
    def __init__(
        self, rows: Iterable[ModelWorkLedgerRowRecord] = (), *, unread: bool = False
    ) -> None:
        self.rows = tuple(rows)
        self.unread = unread
        self.reads = 0

    def owner(self, repo: str, check: str, now: datetime) -> str | None:
        self.reads += 1
        if self.unread:
            raise DevCauseOwnersUnreadError("work ledger down")
        return live_cause_owner(self.rows, repo=repo, check=check, now=now, live=LIVE)


def handler(
    *,
    act: bool = True,
    bus: FakeBusClaims | None = None,
    ledger: FakeLedgerOwners | None = None,
    clock: list[datetime] | None = None,
) -> HandlerDevHeadRedDispatch:
    ticks = clock if clock is not None else [NOW]
    return HandlerDevHeadRedDispatch(
        act=act,
        claims=bus if bus is not None else FakeBusClaims(),
        owners=ledger if ledger is not None else FakeLedgerOwners(),
        lease=LIVE,
        now=lambda: ticks[0],
    )


def split(
    events: Iterable[Any],
) -> tuple[list[ModelDevCauseLaneRequest], ModelDevHeadRedDecided | None]:
    events = list(events)
    requests = [e for e in events if isinstance(e, ModelDevCauseLaneRequest)]
    decided = [e for e in events if isinstance(e, ModelDevHeadRedDecided)]
    assert len(decided) <= 1
    return requests, (decided[0] if decided else None)


async def run(
    h: HandlerDevHeadRedDispatch, event: ModelDevHeadRedEvent | dict[str, Any]
) -> tuple[list[ModelDevCauseLaneRequest], ModelDevHeadRedDecided | None]:
    return split((await h.handle(event)).events)


@pytest.mark.asyncio
async def test_an_unowned_red_asks_for_exactly_one_cause_lane() -> None:
    requests, decided = await run(handler(), red())
    assert len(requests) == 1
    request = requests[0]
    owner_key = dev_head_owner_key(REPO, "dev", CHECK)
    assert owner_key == f"dev:{REPO}:dev:{CHECK}"
    assert request.owner_key == owner_key
    assert request.dispatch_key == dev_cause_dispatch_key(owner_key, SHA)
    # The remote-lane runner's --cause value: <repo>:<check>.
    assert request.cause == "omnimarket:CI Summary"
    assert request.repo == REPO
    assert request.branch == "dev"
    assert request.head_sha == SHA
    assert request.lane.startswith("devred-omnimarket-")
    assert decided is not None
    assert decided.action == EnumDevHeadRedAction.DISPATCH_CAUSE_LANE
    assert decided.dispatch_key == request.dispatch_key
    assert decided.lane == request.lane


@pytest.mark.asyncio
async def test_a_second_identical_event_dispatches_none() -> None:
    h = handler()
    first, _ = await run(h, red())
    assert len(first) == 1
    second, decided = await run(h, red())
    assert second == []
    assert decided is None
    # The same red as a wire payload is the same event.
    third, decided = await run(h, red().model_dump(mode="json"))
    assert third == []
    assert decided is None


@pytest.mark.asyncio
async def test_a_new_head_red_on_a_cause_this_process_dispatched_joins_it() -> None:
    clock = [NOW]
    h = handler(clock=clock)
    assert len((await run(h, red()))[0]) == 1
    requests, decided = await run(h, red(sha="b" * 40))
    assert requests == []
    assert decided is not None
    assert decided.action == EnumDevHeadRedAction.OWNED
    assert decided.owner is not None
    assert decided.owner.startswith("dispatched:devred-")
    # Past the lease the cause is unowned again: the dispatched lane never claimed it.
    clock[0] = NOW + LIVE + timedelta(seconds=1)
    requests, decided = await run(h, red(sha="c" * 40))
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_a_bus_owner_claim_on_the_dev_owner_key_owns_the_cause() -> None:
    bus = FakeBusClaims(owned={dev_head_owner_key(REPO, "dev", CHECK)})
    requests, decided = await run(handler(bus=bus), red())
    assert requests == []
    assert decided is not None
    assert decided.action == EnumDevHeadRedAction.OWNED
    assert decided.owner == "bus-claim:" + dev_head_owner_key(REPO, "dev", CHECK)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cells",
    [
        ("cause=omnimarket:CI Summary",),
        ("cause=omnimarket:ci-summary",),
        ("cause=OmniNode-ai/omnimarket:CI Summary",),
        ("cause=omniclaude:ci-summary", "cause=omnimarket:CI Summary"),
        (
            f"cause={ci_red_cause_key(REPO, CHECK)}",
            "check=unread",
            "relay=landing-controller",
        ),
        (
            "cause=cause:OmniNode-ai/omnimarket:0123456789ab",
            "check=Tests Gate,CI Summary",
            "relay=landing-controller",
        ),
    ],
    ids=[
        "remote-lane",
        "remote-lane-slug-case",
        "full-slug",
        "second-cause-cell",
        "controller-check-key",
        "controller-check-cell",
    ],
)
async def test_a_live_cause_claim_owns_the_cause(cells: tuple[str, ...]) -> None:
    ledger = FakeLedgerOwners(
        [row(NOW - timedelta(minutes=30), "CLAIM", "cause-lane-1", *cells)]
    )
    requests, decided = await run(handler(ledger=ledger), red())
    assert requests == []
    assert decided is not None
    assert decided.action == EnumDevHeadRedAction.OWNED
    assert decided.owner == "ledger-claim:cause-lane-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        # Closed by its lane's TERMINAL.
        [
            row(
                NOW - timedelta(minutes=50),
                "CLAIM",
                "cause-lane-1",
                "cause=omnimarket:CI Summary",
            ),
            row(NOW - timedelta(minutes=5), "TERMINAL", "cause-lane-1", "outcome=done"),
        ],
        # Older than the lease.
        [
            row(
                NOW - LIVE - timedelta(minutes=1),
                "CLAIM",
                "cause-lane-1",
                "cause=omnimarket:CI Summary",
            )
        ],
        # Another repository's cause, another check, and a per-PR CLAIM.
        [
            row(
                NOW - timedelta(minutes=5), "CLAIM", "l1", "cause=omniclaude:CI Summary"
            ),
            row(
                NOW - timedelta(minutes=5), "CLAIM", "l2", "cause=omnimarket:Tests Gate"
            ),
            row(NOW - timedelta(minutes=5), "CLAIM", "l3", "pr=omnimarket#12"),
            row(
                NOW - timedelta(minutes=5),
                "CLAIM",
                "l4",
                "cause=cause:OmniNode-ai/omniclaude:0123456789ab",
                "check=CI Summary",
            ),
        ],
    ],
    ids=["closed", "expired", "other-causes"],
)
async def test_a_claim_that_does_not_own_this_cause_does_not_block(
    rows: list[ModelWorkLedgerRowRecord],
) -> None:
    requests, decided = await run(handler(ledger=FakeLedgerOwners(rows)), red())
    assert len(requests) == 1
    assert decided is not None
    assert decided.action == EnumDevHeadRedAction.DISPATCH_CAUSE_LANE


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["bus", "ledger"])
async def test_an_unreadable_owner_withholds_the_request(which: str) -> None:
    h = handler(
        bus=FakeBusClaims(unread=which == "bus"),
        ledger=FakeLedgerOwners(unread=which == "ledger"),
    )
    requests, decided = await run(h, red())
    assert requests == []
    assert decided is not None
    assert decided.action == EnumDevHeadRedAction.WITHHELD
    assert "withheld:owners-unread" in decided.evidence


@pytest.mark.asyncio
async def test_act_false_records_the_decision_and_asks_for_nothing() -> None:
    ledger = FakeLedgerOwners()
    requests, decided = await run(handler(act=False, ledger=ledger), red())
    assert requests == []
    assert decided is not None
    assert decided.action == EnumDevHeadRedAction.WITHHELD
    assert "withheld:act=false" in decided.evidence


@pytest.mark.asyncio
async def test_an_unbound_work_ledger_is_unread_never_unowned() -> None:
    owners = UnboundDevCauseOwners("OMNINODE_INTERNAL_DB_URL unset")
    with pytest.raises(DevCauseOwnersUnreadError):
        owners.owner(REPO, CHECK, NOW)
    h = HandlerDevHeadRedDispatch(
        act=True, claims=FakeBusClaims(), owners=owners, lease=LIVE, now=lambda: NOW
    )
    requests, decided = await run(h, red())
    assert requests == []
    assert decided is not None
    assert decided.action == EnumDevHeadRedAction.WITHHELD


def test_contract_owner_source_with_its_dsn_unset_is_unbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = yaml.safe_load(CONTRACT.read_text())
    block = raw["dev_head_red_dispatch"]
    monkeypatch.delenv(block["owners"]["dsn_env"], raising=False)
    assert isinstance(
        dev_cause_owners_from_contract(block["owners"]), UnboundDevCauseOwners
    )


def test_contract_routes_dev_head_red_and_declares_the_request_and_decision() -> None:
    raw = yaml.safe_load(CONTRACT.read_text())
    manifest = discover_contracts_from_paths([CONTRACT])
    assert not manifest.errors
    contract = manifest.contracts[0]
    assert contract.handler_routing is not None
    routed = [
        entry
        for entry in contract.handler_routing.handlers
        if tuple(_topics_for_handler_entry(contract, entry)) == (DEV_HEAD_RED_TOPIC_V1,)
    ]
    assert len(routed) == 1
    assert DEV_HEAD_RED_TOPIC_V1 in raw["event_bus"]["subscribe_topics"]
    declared = {row["event_type"]: row["topic"] for row in raw["published_events"]}
    for model, topic in HandlerDevHeadRedDispatch.published_event_topics.items():
        assert declared[model.__name__.removeprefix("Model")] == topic
        assert topic in raw["event_bus"]["publish_topics"]
    assert HandlerDevHeadRedDispatch.published_event_topics == {
        ModelDevCauseLaneRequest: DEV_CAUSE_LANE_REQUESTED_TOPIC_V1,
        ModelDevHeadRedDecided: DEV_HEAD_RED_DECIDED_TOPIC_V1,
    }
    block = raw["dev_head_red_dispatch"]
    assert block["lease_seconds"] == 5400
    assert block["owners"] == {
        "relation": "omninode_internal.work_ledger_rows",
        "dsn_env": "OMNINODE_INTERNAL_DB_URL",
    }
    assert block["act"] == "${env.ONEX_DEV_HEAD_RED_DISPATCH_ACT:true}"


@pytest.mark.asyncio
async def test_replay_one_red_dev_head_observation_dispatches_one_lane_then_none() -> (
    None
):
    """The lab replay, in process: webhook branch-head observation -> detector -> dispatcher."""
    observation = {
        "kind": "branch-head-status",
        "entity_id": f"{REPO}@dev",
        "repo": REPO,
        "branch": "dev",
        "delivery_id": "6f1c2d3e-0000-4000-8000-000000000001",
        "github_event": "check_run",
        "as_of": "2026-10-13T21:42:00Z",
        "sha": SHA,
        "ci_status": "FAILURE",
        "check_name": CHECK,
    }
    detector = HandlerDetectDevHeadRed()
    dispatcher = handler()
    dispatched: list[ModelDevCauseLaneRequest] = []
    for _ in range(2):
        for event in (await detector.handle(observation)).events:
            dispatched += (await run(dispatcher, event.model_dump(mode="json")))[0]
    assert len(dispatched) == 1
    # The dispatcher alone, handed the same red twice (a redelivery past the detector), still asks once.
    DEV_HEAD_RED_INDEX.clear()
    again = list((await detector.handle(observation)).events)
    assert len(again) == 1
    assert (await run(dispatcher, again[0].model_dump(mode="json")))[0] == []
