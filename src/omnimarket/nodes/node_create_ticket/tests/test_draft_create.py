# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exercise draft create with the real handler and a record-shaped Linear transport."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from contextlib import nullcontext
from pathlib import Path
from threading import Barrier, Lock
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import yaml
from click.testing import CliRunner
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.models.ticket.model_ticket_draft import ModelTicketDraft
from omnibase_core.runtime.runtime_dispatch import RuntimeDispatch
from omnibase_core.runtime.transport.runtime_in_memory_broker import InMemoryBroker
from omnibase_core.runtime.transport.runtime_in_memory_transport import (
    InMemoryTransport,
)
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.core_runtime.routing_map_builder import (
    DefBTarget,
    build_routing_map,
)

from omnimarket.handlers.ticket import ticket_cli
from omnimarket.models.model_ticket_contract_render import (
    EnumTicketContractRenderOperation,
    ModelTicketContractRenderRequest,
)
from omnimarket.models.model_ticket_io import ModelCreateTicketRequest
from omnimarket.models.ticket_write_io import ModelTicketWriteRequest
from omnimarket.nodes.node_create_ticket import HandlerCreateTicket
from omnimarket.nodes.node_create_ticket.handlers.core_ticket_guard import (
    CoreTicketGuard,
)
from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    LinearTicketHttpGateway,
    ModelTicketGuardDecision,
)
from omnimarket.nodes.node_linear_ticket_write_effect.handlers import (
    HandlerLinearTicketWriteEffect,
)
from omnimarket.nodes.node_ticket_contract_render_compute import (
    NodeTicketContractRenderCompute,
)

pytestmark = pytest.mark.unit
_CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "node_linear_ticket_write_effect/contract.yaml"
)
_WRITTEN = "onex.evt.omnimarket.ticket-contract-written.v1"
_COMPLETED = "onex.evt.omnimarket.linear-ticket-write-completed.v1"
_FAILED = "onex.evt.omnimarket.linear-ticket-write-failed.v1"
_STAMP = "created-by-lane: example-lane"
_DRAFT_ID = UUID("84f13245-068d-4d03-87b4-a93d6ad9beaf")
_UPDATED = "2026-01-17T13:50:40.123Z"


def _request(**changes: object) -> dict[str, Any]:
    draft = ModelTicketDraft.model_validate(
        {
            "title": "A typed draft",
            "business_brief": {
                "what_this_is": "A fixture for the typed draft.",
                "why_it_matters": "The same contract is kept in both places.",
                "done_looks_like": "The rendered body parses.",
                "risk": "The tracker could normalize Markdown.",
                "depends_on": "The contract renderer.",
            },
            "parent_ticket_id": "OMN-90001",
            "parent_ac_label": "AC1",
            "repository": "draft-default",
            "proof_class": "code-only",
            "requirements": [
                {
                    "id": "REQ-1",
                    "statement": "The contract is written.",
                    "acceptance": [
                        {
                            "id": "AC1",
                            "statement": "the rendered body parses",
                            "falsifier": "uv run pytest tests/example.py",
                            "integrated": False,
                        }
                    ],
                }
            ],
            "dod_evidence": [
                {
                    "id": "proof",
                    "description": "The rendered contract parses.",
                    "binds_ac": ["AC1"],
                    "checks": [
                        {
                            "check_type": "test_passes",
                            "check_value": "tests/example.py",
                        }
                    ],
                }
            ],
        }
    )
    return {
        "title": draft.title,
        "description": _STAMP,
        "draft": draft,
        "draft_id": _DRAFT_ID,
        "repo": "omnimarket",
        **changes,
    }


class AssumedLinear(LinearTicketHttpGateway):
    """The sandbox must measure client UUID acceptance, errors and Markdown normalization."""

    def __init__(self) -> None:
        super().__init__("test-only")
        self.calls: list[str] = []
        self.issues: dict[str, dict[str, Any]] = {}
        self.refuse_create = False
        self.fail_clear = False
        self.fail_after_create = False
        self.fail_after_write = False
        self.edit_before_write = False
        self.edit_after_write = False
        self.normalize_readback = False
        self.writes: list[str] = []
        self.created_descriptions: list[str] = []

    def _post(self, query: str, variables: dict[str, object]) -> Any:
        operation = query.split()[1].partition("(")[0]
        self.calls.append(operation)
        if operation == "GetTeamByName":
            return {"data": {"teams": {"nodes": [{"id": "team"}]}}}
        if operation == "GetBacklogState":
            return {"data": {"workflowStates": {"nodes": [{"id": "backlog"}]}}}
        if operation == "GetViewer":
            return {"data": {"viewer": {"id": "viewer"}}}
        if "issueLabels(" in query:
            return {"data": {"issueLabels": {"nodes": [{"id": "intake"}]}}}
        if operation == "GetIssueByIdentifier":
            return {"data": {"issue": {"id": "parent"}}}
        if operation == "CreateDraftIssue":
            data = variables["input"]
            assert isinstance(data, dict)
            issue_id = data["id"]
            assert UUID(issue_id).version == 4
            assert data["projectId"] is None
            assert data["parentId"] == "parent"
            if self.refuse_create or issue_id in self.issues:
                return {
                    "errors": [
                        {"message": "duplicate id", "extensions": {"code": "CONFLICT"}}
                    ],
                    "data": None,
                }
            created_issue = {
                "id": issue_id,
                "title": data["title"],
                "identifier": "OMN-90002",
                "url": "https://linear.app/example/issue/OMN-90002",
                "description": data["description"],
                "createdAt": "2026-01-17T13:49:00.000Z",
                "updatedAt": _UPDATED,
                "project": {"name": "Inherited"},
            }
            self.issues[issue_id] = created_issue
            self.created_descriptions.append(data["description"])
            if self.fail_after_create:
                self.fail_after_create = False
                return {"errors": [{"message": "create response lost"}], "data": None}
            return {
                "data": {"issueCreate": {"success": True, "issue": dict(created_issue)}}
            }
        issue = self.issues.get(str(variables.get("id")))
        if issue is None:
            issue = next(
                (
                    row
                    for row in self.issues.values()
                    if row["identifier"] == variables.get("id")
                ),
                None,
            )
        if operation == "ReadDraftIssue":
            if (
                issue is not None
                and self.edit_before_write
                and "GetIssueProject" in self.calls
            ):
                issue["description"] += "\nA hand edit before the description write."
                self.edit_before_write = False
            return {"data": {"issue": dict(issue) if issue else None}}
        assert issue is not None, (operation, variables)
        if operation == "GetIssueProject":
            return {"data": {"issue": dict(issue)}}
        if operation == "ClearIssueProject":
            if self.fail_clear:
                self.fail_clear = False
                return {"data": {"issueUpdate": {"success": False}}}
            issue["project"] = None
        elif operation == "WriteDraftDescription":
            assert issue["project"] is None
            description = variables["description"]
            assert isinstance(description, str)
            self.writes.append(description)
            if self.normalize_readback:
                description = description.rstrip().replace("\n", "\r\n")
                issue["title"] += " "
            if self.edit_after_write:
                description += "\nA hand edit after the description write."
            issue["description"] = description
            if self.fail_after_write:
                self.fail_after_write = False
                return {"data": {"issueUpdate": {"success": False}}}
        else:
            raise AssertionError(f"unexpected GraphQL operation {operation!r}")
        return {"data": {"issueUpdate": {"success": True, "issue": dict(issue)}}}

    def handler(self, lane: str) -> HandlerCreateTicket:
        return HandlerCreateTicket(
            linear_client=self, lane_id=lane, require_description_stamp=True
        )


class RacingLinear(AssumedLinear):
    """Force both workers past their first lookup before either can create."""

    def __init__(self) -> None:
        super().__init__()
        self._mutation_lock = Lock()
        self._first_lookups = Barrier(2, timeout=5)
        self._read_count = 0

    def _post(self, query: str, variables: dict[str, object]) -> Any:
        wait_for_peer = False
        with self._mutation_lock:
            response = super()._post(query, variables)
            if "query ReadDraftIssue(" in query:
                self._read_count += 1
                wait_for_peer = self._read_count <= 2
        if wait_for_peer:
            assert response == {"data": {"issue": None}}
            self._first_lookups.wait()
        return response


class RecordingTransport(InMemoryTransport):
    def __init__(self, broker: InMemoryBroker, command: str) -> None:
        super().__init__(broker=broker, group="draft-proof", topics=[command])
        self.recorded: list[str] = []
        self.fail_once: str | None = None

    async def send(
        self, topic: str, key: bytes | None, value: bytes, headers: Mapping[str, bytes]
    ) -> None:
        if topic == self.fail_once:
            self.fail_once = None
            self.recorded.append(f"unacknowledged:{topic}")
            raise RuntimeError("broker acknowledgement lost")
        await super().send(topic, key=key, value=value, headers=headers)
        self.recorded.append(topic)

    async def commit(self, message: object) -> None:
        await super().commit(message)
        self.recorded.append("commit")


class DraftBus:
    def __init__(self, linear: AssumedLinear) -> None:
        manifest = discover_contracts_from_paths([_CONTRACT])
        assert not manifest.errors
        (contract,) = manifest.contracts
        assert contract.event_bus is not None
        self.command = contract.event_bus.subscribe_topics[0]
        self.broker = InMemoryBroker(num_partitions=1)
        self.transport = RecordingTransport(self.broker, self.command)
        effect = HandlerLinearTicketWriteEffect(handler_factory=linear.handler)
        routes = build_routing_map(
            [contract],
            frozenset([self.command]),
            handler_resolver=lambda _: cast(DefBTarget, effect),
        )
        # Core's consumer mirror and the concrete test transport expose the same
        # fields, but mypy treats the mirror's headers attribute as invariant.
        self.runtime = RuntimeDispatch(
            consumer=cast(Any, self.transport),
            producer=self.transport,
            routing_map=routes,
        )

    async def submit(self, **changes: object) -> UUID:
        correlation = uuid4()
        request = ModelTicketWriteRequest.model_validate(
            {
                **_request(**changes),
                "correlation_id": correlation,
                "idempotency_key": "draft-fixture",
                "requested_by_lane": "example-lane",
            }
        )
        envelope = ModelEventEnvelope[dict[str, Any]](
            payload=request.model_dump(mode="json"), correlation_id=correlation
        )
        await self.transport.send(
            self.command,
            key=b"draft",
            value=envelope.model_dump_json().encode(),
            headers={},
        )
        return correlation

    def payloads(self, topic: str) -> list[dict[str, Any]]:
        return [
            json.loads(record.value)["payload"]
            for record in self.broker.records(topic, 0)
        ]


def test_draft_create() -> None:
    linear = AssumedLinear()
    request = ModelCreateTicketRequest.model_validate(_request())
    result = linear.handler("example-lane").handle(request)
    parsed = NodeTicketContractRenderCompute().handle(
        ModelTicketContractRenderRequest(
            operation=EnumTicketContractRenderOperation.PARSE, body=linear.writes[0]
        )
    )
    assert not parsed.findings
    assert parsed.contract == result.contract
    assert parsed.contract is not None
    assert request.draft is not None
    assert {
        field: getattr(parsed.contract, field)
        for field in ModelTicketDraft.model_fields
    } == {**dict(request.draft), "repository": "omnimarket"}
    assert result.route == "draft"
    assert parsed.contract.ticket_id == "OMN-90002"
    assert (
        parsed.block_fingerprint
        == parsed.contract.compute_fingerprint()
        == result.fingerprint
    )


async def test_draft_create_retry_single_issue() -> None:
    linear = AssumedLinear()
    linear.fail_clear = True
    first = DraftBus(linear)
    await first.submit()
    assert await first.runtime.drain() == 1
    assert first.payloads(_FAILED)[0]["reason"] == "linear_error"
    assert not first.payloads(_WRITTEN)
    retry = DraftBus(linear)
    await retry.submit()
    assert await retry.runtime.drain() == 1
    assert (
        len(linear.issues) == len(linear.writes) == len(retry.payloads(_COMPLETED)) == 1
    )


async def test_draft_create_publishes_written_assumes_linear_markdown_round_trip() -> (
    None
):
    linear = AssumedLinear()
    linear.normalize_readback = True
    bus = DraftBus(linear)
    await bus.submit()
    assert await bus.runtime.drain() == 1
    (event,) = bus.payloads(_WRITTEN)
    (readback,) = linear.issues.values()
    raw = json.dumps(
        [readback["title"], readback["description"]],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    assert readback["description"] != linear.writes[0]
    assert event["content_sha256"] == hashlib.sha256(raw).hexdigest()
    assert event["updated_at"] == _UPDATED
    assert event["ticket_id"] == "OMN-90002"
    assert event["fingerprint"] == bus.payloads(_COMPLETED)[0]["result"]["fingerprint"]
    assert bus.transport.recorded == [bus.command, _WRITTEN, _COMPLETED, "commit"]


async def test_draft_create_different_envelopes_one_issue_assumes_linear_client_uuid() -> (
    None
):
    linear = RacingLinear()
    first, second = DraftBus(linear), DraftBus(linear)
    first_id, second_id = await asyncio.gather(first.submit(), second.submit())
    assert first_id != second_id
    first_steps, second_steps = await asyncio.gather(
        first.runtime.drain(), second.runtime.drain()
    )
    assert first_steps == second_steps == 1
    assert linear.calls.count("CreateDraftIssue") == 2
    assert len(linear.issues) == 1
    assert len(set(linear.writes)) == 1
    assert [
        terminal["correlation_id"]
        for terminal in first.payloads(_COMPLETED) + second.payloads(_COMPLETED)
    ] == [
        str(first_id),
        str(second_id),
    ]
    events = first.payloads(_WRITTEN) + second.payloads(_WRITTEN)
    assert len(events) == 2
    assert len({event["event_id"] for event in events}) == 1


async def test_draft_create_one_lookup_after_refusal_assumes_linear_duplicate_error_shape() -> (
    None
):
    linear = AssumedLinear()
    linear.refuse_create = True
    bus = DraftBus(linear)
    await bus.submit()
    assert await bus.runtime.drain() == 1
    assert linear.calls.count("CreateDraftIssue") == 1
    assert linear.calls.count("ReadDraftIssue") == 2  # initial lookup and one relookup
    assert bus.payloads(_FAILED)[0]["reason"] == "id_unreadable"
    assert not linear.writes


async def test_draft_create_recovers_when_create_takes_effect_before_error() -> None:
    linear = AssumedLinear()
    linear.fail_after_create = True
    bus = DraftBus(linear)
    await bus.submit()
    assert await bus.runtime.drain() == 1
    assert len(linear.issues) == len(linear.writes) == 1
    assert linear.calls.count("CreateDraftIssue") == 1
    assert len(bus.payloads(_COMPLETED)) == 1


async def test_draft_create_clears_inherited_project_on_fresh_and_retry() -> None:
    linear = AssumedLinear()
    bus = DraftBus(linear)
    await bus.submit()
    assert await bus.runtime.drain() == 1
    (issue,) = linear.issues.values()
    issue["project"] = {"name": "Inherited again"}
    await bus.submit()
    assert await bus.runtime.drain() == 1
    assert linear.calls.count("ClearIssueProject") == 2
    assert issue["project"] is None
    assert len(linear.writes) == 1
    assert len(bus.payloads(_COMPLETED)) == 2


async def test_draft_create_rebuilds_deterministically_and_refuses_changed_payload() -> (
    None
):
    linear = AssumedLinear()
    linear.fail_after_write = True
    bus = DraftBus(linear)
    await bus.submit()
    assert await bus.runtime.drain() == 1
    assert not bus.payloads(_WRITTEN)
    await bus.submit()
    assert await bus.runtime.drain() == 1
    assert len(linear.writes) == len(bus.payloads(_WRITTEN)) == 1

    await bus.submit(repo="different-repo")
    assert await bus.runtime.drain() == 1
    assert bus.payloads(_FAILED)[-1]["reason"] == "draft_payload_conflict"
    assert len(linear.writes) == len(bus.payloads(_WRITTEN)) == 1


async def test_draft_create_withholds_commit_until_terminal_ack_and_keeps_event_id() -> (
    None
):
    linear = AssumedLinear()
    bus = DraftBus(linear)
    bus.transport.fail_once = _COMPLETED
    await bus.submit()
    await bus.transport.start()
    assert await bus.runtime.run_once() == 1
    assert bus.transport.recorded == [
        bus.command,
        _WRITTEN,
        f"unacknowledged:{_COMPLETED}",
    ]
    assert not bus.payloads(_COMPLETED)
    assert await bus.runtime.run_once() == 1
    assert bus.transport.recorded[-3:] == [_WRITTEN, _COMPLETED, "commit"]
    first, second = bus.payloads(_WRITTEN)
    assert first["event_id"] == second["event_id"]
    assert len(linear.writes) == 1


async def test_draft_create_withholds_completed_and_commit_until_written_ack() -> None:
    linear = AssumedLinear()
    bus = DraftBus(linear)
    bus.transport.fail_once = _WRITTEN
    await bus.submit()
    await bus.transport.start()
    assert await bus.runtime.run_once() == 1
    assert bus.transport.recorded == [bus.command, f"unacknowledged:{_WRITTEN}"]
    assert not bus.payloads(_COMPLETED)
    assert await bus.runtime.run_once() == 1
    assert bus.transport.recorded[-3:] == [_WRITTEN, _COMPLETED, "commit"]


async def test_draft_create_keeps_the_stamp_on_retry() -> None:
    linear = AssumedLinear()
    bus = DraftBus(linear)
    await bus.submit()
    assert await bus.runtime.drain() == 1
    (stub,) = linear.created_descriptions
    (issue,) = linear.issues.values()
    stamp = stub.splitlines()[1]
    assert str(_DRAFT_ID) in stamp
    assert issue["description"].splitlines()[1] == stamp

    await bus.submit()
    assert await bus.runtime.drain() == 1
    assert len(linear.writes) == 1
    assert len(bus.payloads(_COMPLETED)) == 2
    assert issue["description"].splitlines()[1] == stamp


@pytest.mark.parametrize("stamp", [_STAMP, _STAMP + "\r\n"])
def test_draft_create_writes_both_stamps_and_guards_final_body(stamp: str) -> None:
    request = ModelCreateTicketRequest.model_validate(_request(description=stamp))
    linear = AssumedLinear()
    guard = CoreTicketGuard("example-lane")
    guarded: list[str] = []

    class GuardSpy:
        def check(self, payload: dict[str, object]) -> ModelTicketGuardDecision:
            guarded.append(str(payload["description"]))
            return guard.check(payload)

    handler = HandlerCreateTicket(
        linear_client=linear,
        lane_id="example-lane",
        ticket_guard=GuardSpy(),
        require_description_stamp=True,
    )
    handler.handle(request)
    (sent,) = linear.writes
    assert sent == guarded[-1]
    assert sent.splitlines()[0] == _STAMP
    assert sent.splitlines()[1] == linear.created_descriptions[0].splitlines()[1]
    assert str(_DRAFT_ID) in sent.splitlines()[1]


async def test_draft_create_hand_edit_on_readback_gives_no_written_event() -> None:
    linear = AssumedLinear()
    linear.edit_after_write = True
    bus = DraftBus(linear)
    await bus.submit()
    assert await bus.runtime.drain() == 1
    assert not bus.payloads(_WRITTEN)
    assert not bus.payloads(_COMPLETED)
    assert bus.payloads(_FAILED)[0]["reason"] == "hand_edited"


async def test_draft_create_refuses_drift_after_assumed_linear_markdown_normalization() -> (
    None
):
    linear = AssumedLinear()
    linear.normalize_readback = True
    bus = DraftBus(linear)
    await bus.submit()
    assert await bus.runtime.drain() == 1
    (issue,) = linear.issues.values()
    assert "\r\n" in issue["description"]
    assert len(bus.payloads(_WRITTEN)) == 1

    old_body = issue["description"]
    issue["description"] = old_body.replace(
        "The rendered body parses.", "The rendered text was edited.", 1
    )
    assert issue["description"] != old_body
    await bus.submit()
    assert await bus.runtime.drain() == 1
    assert bus.payloads(_FAILED)[0]["reason"] == "hand_edited"
    assert len(bus.payloads(_WRITTEN)) == len(linear.writes) == 1


async def test_draft_create_rereads_before_write_and_leaves_hand_edit_alone() -> None:
    linear = AssumedLinear()
    linear.edit_before_write = True
    bus = DraftBus(linear)
    await bus.submit()
    assert await bus.runtime.drain() == 1
    assert not linear.writes
    assert linear.calls[-1] == "ReadDraftIssue"
    assert bus.payloads(_FAILED)[0]["reason"] == "hand_edited"


def test_draft_create_cli_reuses_draft_id_and_rotates_correlation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[ModelTicketWriteRequest] = []

    async def call_topics(**kwargs: Any) -> Any:
        sent.append(kwargs["request"])
        return SimpleNamespace(failed=False, model_dump_json=lambda: "{}")

    monkeypatch.setattr(ticket_cli, "_open_bus", lambda _: nullcontext(object()))
    monkeypatch.setattr(
        ticket_cli, "ScmBusCaller", lambda _: SimpleNamespace(call_topics=call_topics)
    )
    path = tmp_path / "draft.yaml"
    path.write_text(
        yaml.safe_dump(_request()["draft"].model_dump(mode="json")), encoding="utf-8"
    )
    runner = CliRunner()
    args = [
        "create",
        "--draft",
        str(path),
        "--repo",
        "omnimarket",
        "--lane",
        "example-lane",
    ]
    for extra in ([], [], ["--draft-id", str(_DRAFT_ID)]):
        result = runner.invoke(ticket_cli.ticket_group, args + extra)
        assert result.exit_code == 0, result.output
    first, second, overridden = sent
    assert first.draft is not None
    assert first.repo == "omnimarket"
    assert first.draft_id == second.draft_id
    assert first.correlation_id != second.correlation_id
    assert overridden.draft_id == _DRAFT_ID
