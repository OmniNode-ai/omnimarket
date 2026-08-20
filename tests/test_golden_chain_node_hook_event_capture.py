# SPDX-License-Identifier: MIT
"""Golden chain: gateway wire envelope -> node_hook_event_capture -> hook_events.

This drives the **producer's** wire shape through the consumer, not a shape
invented here. The producer is the onex-api Secure Workflow Gateway in the
private ``omninode_infra`` repo; its payload is built by
``to_canonical_wire_envelope``, which for this workflow_type emits exactly:

    the catalog passthrough keys, in catalog property order
        source, batch_sha, events
    plus the fields injected on EVERY workflow envelope
        correlation_id, emitted_at
    plus the attribution slug, when a tenant slug is resolved
        tenant_id
    plus the immutable principal, because hook-event-capture is a member of
    _WIRE_PAYLOAD_TENANT_PRINCIPAL_WORKFLOWS
        tenant_principal_id

That producer cannot be imported here (different, private repo), so the shape
is reproduced as a literal with the injection order preserved, and a test below
asserts the consumer accepts NOTHING outside it — which is what makes the
reproduction falsifiable rather than decorative. If the gateway leg ever adds a
field, ``extra="forbid"`` turns that into a loud failure at this seam instead of
a silent drop in production.

``handle()`` (called through this file with the runtime-injected dict shape —
payload plus ``_db``/``_event_type``/``_topic``) is the ONLY entrypoint the
real projection dispatch arm ever calls for this node (OMN-16090). The
end-to-end proof that the arm itself dispatches this shape correctly lives in
``test_omn16090_real_dispatch_path.py``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_hook_event_capture.handlers.handler_hook_event_capture import (
    HandlerHookEventCapture,
    HookEventCaptureError,
)
from omnimarket.nodes.node_hook_event_capture.models.model_hook_event_capture_request import (
    ModelHookEventCaptureRequest,
)

pytestmark = pytest.mark.unit

COMMAND_TOPIC = "onex.cmd.omnimarket.hook-event-capture-requested.v1"
PRINCIPAL = "t-" + "a" * 32

# The four event families measured in the real operator spool corpus
# (n=1931, 2026-08-16). Two of them carry no event_id at all — which is the
# whole reason event_sha is the dedupe key.
REAL_FAMILIES: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "artifact.captured",
        {
            "artifact_ref": "sha256:e7c173c7222421010fcca674d73a88fe02ddaa45acfbff0873c80f9dbb5161a3",
            "artifact_kind": "runtime_capture_log",
            "artifact_size_bytes": 197,
            "correlation_id": "89d7703e-6626-48ae-9a45-035f81ca0c1d",
            "run_id": "0049ccb4-6c0b-4355-90b5-fe30be548b06",
            "redaction_state": "raw",
        },
    ),
    (
        "tool.output.captured",
        {
            "tool_name": "Bash",
            "node_name": "node_dod_verify",
            "exit_code": 0,
            "status": "success",
            "capture_log_bytes": 512,
            "correlation_id": "11111111-2222-3333-4444-555555555555",
            "run_id": "66666666-7777-8888-9999-000000000000",
        },
    ),
    (
        "onex.evt.omniclaude.skill-started.v1",
        {
            "event_id": "78a2d73c-05f7-43c9-a924-a818ea56bd55",
            "run_id": "017a6b48-6f9b-4651-8a42-6b755e80e386",
            "skill_name": "node_dod_verify",
            "repo_id": "omnibase_infra",
            "correlation_id": "694efd9b-ac41-40a1-8923-4d2954d84904",
            "emitted_at": "2026-08-09T15:35:58.797727+00:00",
        },
    ),
    (
        "onex.evt.omniclaude.skill-completed.v1",
        {
            "event_id": "619a801f-2d28-4460-8068-87f4a1a91f35",
            "run_id": "01802cca-a645-4d48-96c4-ed4dfbf46aa7",
            "skill_name": "node_dod_verify",
            "repo_id": "omnibase_infra",
            "status": "success",
            "duration_ms": 68497,
            "emitted_at": "2026-08-04T18:25:17.184528+00:00",
        },
    ),
)


class InmemoryHookEventsTable:
    """Stands in for hook_events with the migration's real conflict target.

    Implements the injected ``DatabaseAdapter`` protocol (single-row
    ``upsert``/``query``) that the real projection dispatch arm hands
    ``handle()`` — see ``handler_hook_event_capture``'s module docstring for
    why the handler queries before writing instead of relying on a SQL-level
    ``DO NOTHING``.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    def upsert(self, table: str, conflict_key: str, row: dict[str, Any]) -> bool:
        key = (str(row["tenant_id"]), str(row["event_sha"]))
        self.rows[key] = dict(row)
        return True

    def query(
        self, table: str, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        applied = filters or {}
        return [
            dict(row)
            for row in self.rows.values()
            if all(row.get(k) == v for k, v in applied.items())
        ]


def _event_sha(event_type: str, payload: dict[str, Any], occurred_at: str) -> str:
    blob = json.dumps(
        {"event_type": event_type, "payload": payload, "occurred_at": occurred_at},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def gateway_wire_payload() -> dict[str, Any]:
    """The payload the gateway publishes, key order preserved."""
    events: list[dict[str, Any]] = []
    for event_type, body in REAL_FAMILIES:
        occurred_at = body.get("emitted_at", "2026-06-28T17:25:25.517566+00:00")
        event: dict[str, Any] = {
            "event_type": event_type,
            "event_sha": _event_sha(event_type, body, occurred_at),
            "occurred_at": occurred_at,
            "payload_json": json.dumps(body, sort_keys=True, separators=(",", ":")),
        }
        if "event_id" in body:
            event["event_id"] = body["event_id"]
        if "correlation_id" in body:
            event["correlation_id"] = body["correlation_id"]
        if "run_id" in body:
            event["run_id"] = body["run_id"]
        event["spooled_at"] = occurred_at
        event["spool_reason"] = "FileNotFoundError: [Errno 2] No such file or directory"
        events.append(event)

    batch_sha = hashlib.sha256(
        "\n".join(e["event_sha"] for e in events).encode()
    ).hexdigest()
    return {
        "source": "local_macos_claude_hooks",
        "batch_sha": batch_sha,
        "events": events,
        "correlation_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "emitted_at": "2026-08-16T18:00:00Z",
        "tenant_id": "omninode",
        "tenant_principal_id": PRINCIPAL,
    }


def _dispatch_input(
    batch_payload: dict[str, Any], db: InmemoryHookEventsTable
) -> dict[str, Any]:
    """Reproduce exactly what the projection dispatch arm hands to handle()."""
    return {
        **batch_payload,
        "_db": db,
        "_event_type": "hook-event-capture-requested",
        "_topic": COMMAND_TOPIC,
    }


@pytest.fixture
def chain() -> tuple[HandlerHookEventCapture, InmemoryHookEventsTable]:
    return HandlerHookEventCapture(), InmemoryHookEventsTable()


class TestGoldenChain:
    def test_gateway_payload_lands_as_rows(
        self, chain: tuple[HandlerHookEventCapture, InmemoryHookEventsTable]
    ) -> None:
        handler, table = chain
        handler.handle(_dispatch_input(gateway_wire_payload(), table))
        assert len(table.rows) == len(REAL_FAMILIES)
        stored = {r["event_type"] for r in table.rows.values()}
        assert stored == {family for family, _ in REAL_FAMILIES}

    def test_payload_body_is_stored_verbatim(
        self, chain: tuple[HandlerHookEventCapture, InmemoryHookEventsTable]
    ) -> None:
        """A capture surface that mangles the body has captured nothing."""
        handler, table = chain
        handler.handle(_dispatch_input(gateway_wire_payload(), table))
        by_type = {r["event_type"]: r["payload"] for r in table.rows.values()}
        for family, body in REAL_FAMILIES:
            assert by_type[family] == body

    def test_families_without_event_id_still_land(
        self, chain: tuple[HandlerHookEventCapture, InmemoryHookEventsTable]
    ) -> None:
        """61% of the real corpus has no event_id — it must not be dropped."""
        handler, table = chain
        handler.handle(_dispatch_input(gateway_wire_payload(), table))
        no_id = [
            r
            for r in table.rows.values()
            if r["event_type"] in ("artifact.captured", "tool.output.captured")
        ]
        assert len(no_id) == 2
        assert all(r["event_id"] is None for r in no_id)
        assert all(len(r["event_sha"]) == 64 for r in no_id)

    def test_replay_of_the_whole_chain_is_a_no_op(
        self, chain: tuple[HandlerHookEventCapture, InmemoryHookEventsTable]
    ) -> None:
        handler, table = chain
        payload = gateway_wire_payload()
        handler.handle(_dispatch_input(payload, table))
        snapshot = dict(table.rows)
        handler.handle(_dispatch_input(gateway_wire_payload(), table))
        assert table.rows == snapshot

    def test_handle_returns_the_typed_result_shape(
        self, chain: tuple[HandlerHookEventCapture, InmemoryHookEventsTable]
    ) -> None:
        """``handle()`` is the canonical def-B dispatch entrypoint auto-wiring
        binds. It returns a dict (the JSON-mode dump of
        ``ModelHookEventCaptureResult`` plus ``rows_upserted`` for the arm's
        own terminal-event gating) — see the handler module docstring for why
        the INPUT is a dict too, not the typed request model."""
        handler, table = chain
        payload = gateway_wire_payload()
        result = handler.handle(_dispatch_input(payload, table))

        assert result["batch_sha"] == payload["batch_sha"]
        assert result["events_received"] == len(REAL_FAMILIES)
        assert result["events_persisted"] == len(REAL_FAMILIES)
        assert result["events_already_present"] == 0
        assert result["rows_upserted"] == len(REAL_FAMILIES)
        assert len(table.rows) == len(REAL_FAMILIES)

    def test_result_distinguishes_a_replay_from_new_work(
        self, chain: tuple[HandlerHookEventCapture, InmemoryHookEventsTable]
    ) -> None:
        """A caller that cannot tell these apart cannot tell progress from a stall."""
        handler, table = chain
        payload = gateway_wire_payload()
        first = handler.handle(_dispatch_input(payload, table))
        second = handler.handle(_dispatch_input(payload, table))

        assert first["events_persisted"] == len(REAL_FAMILIES)
        assert first["events_already_present"] == 0
        assert second["events_persisted"] == 0
        assert second["events_already_present"] == len(REAL_FAMILIES)

    def test_writer_stamps_the_row_tenant(
        self, chain: tuple[HandlerHookEventCapture, InmemoryHookEventsTable]
    ) -> None:
        handler, table = chain
        handler.handle(_dispatch_input(gateway_wire_payload(), table))
        assert {r["tenant_id"] for r in table.rows.values()} == {"omninode"}


class TestSeamIsFalsifiable:
    """What makes the reproduced producer shape above worth anything."""

    def test_consumer_accepts_exactly_the_gateway_field_set(self) -> None:
        model = ModelHookEventCaptureRequest.model_validate(gateway_wire_payload())
        assert set(model.model_fields_set) == {
            "source",
            "batch_sha",
            "events",
            "correlation_id",
            "emitted_at",
            "tenant_id",
            "tenant_principal_id",
        }

    def test_an_unknown_gateway_field_fails_loudly_here(self) -> None:
        """A gateway-side addition must break at this seam, not silently drop."""
        payload = gateway_wire_payload()
        payload["context_pack"] = "delegation-only field"
        with pytest.raises(ValidationError):
            ModelHookEventCaptureRequest.model_validate(payload)

    def test_missing_principal_is_poison_not_a_silent_default(
        self, chain: tuple[HandlerHookEventCapture, InmemoryHookEventsTable]
    ) -> None:
        handler, table = chain
        payload = gateway_wire_payload()
        del payload["tenant_principal_id"]
        with pytest.raises(HookEventCaptureError):
            handler.handle(_dispatch_input(payload, table))
        assert table.rows == {}

    def test_contract_subscribes_to_the_topic_this_chain_drives(self) -> None:
        contract = yaml.safe_load(
            (
                __import__("pathlib").Path(__file__).resolve().parents[1]
                / "src/omnimarket/nodes/node_hook_event_capture/contract.yaml"
            ).read_text()
        )
        assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
        # Exactly ONE published topic: the batch-completion signal. NOT a
        # re-emission of the captured events onto their original evt topics —
        # that would mean fanning one submission across N caller-named topics,
        # the injection shape the gateway exists to prevent.
        assert contract["event_bus"]["publish_topics"] == [
            "onex.evt.omnimarket.hook-events-captured.v1"
        ]
        assert contract["terminal_event"] == (
            "onex.evt.omnimarket.hook-events-captured.v1"
        )
