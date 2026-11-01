# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The serve takes newly declared machines as writers without a reinstall (OMN-21622)."""

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolBusMessage,
    event_type_for,
)
from omnimarket.handlers.work_ledger_consume.writer_overlay_watch import (
    ModelWriterOverlayPolicy,
    WriterOverlayWatch,
    follow_overlay_writers,
    load_writer_overlay_policy,
    overlay_path,
    overlay_writers,
)
from omnimarket.nodes.node_work_ledger_append_effect import (
    EnumWorkLedgerAppendStatus,
    HandlerWorkLedgerAppendEffect,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    ModelAppendCommandResult,
)
from omnimarket.work_ledger_bus.bus import (
    WorkLedgerAppendHost,
    load_work_ledger_append_topics,
)

pytestmark = pytest.mark.unit

PATTERN = load_work_ledger_append_topics().writer_principal_pattern


def _overlay(*rows: dict[str, str]) -> str:
    return yaml.safe_dump(
        {
            "schema_version": "developer_principal.v1",
            "grants_source": "deploy/lab/developer-principal-grants.yaml",
            "principals": list(rows),
        }
    )


def _row(principal: str, role: str | None = None) -> dict[str, str]:
    row = {"principal": principal, "login": "someone@example.com", "device": "box"}
    if role is not None:
        row["role"] = role
    return row


def test_the_contract_declares_machine_rows_as_writers() -> None:
    policy = load_writer_overlay_policy()
    assert policy.roles == ("machine",)
    assert policy.default_role == "developer"
    assert overlay_path(policy, {}) is None
    assert overlay_path(policy, {policy.root_env: "/cfg"}) == Path("/cfg") / policy.path


def test_only_rows_of_a_writing_role_are_writers() -> None:
    policy = load_writer_overlay_policy()
    text = _overlay(
        _row("dev-person-laptop"),
        _row("dev-person-desk", "developer"),
        _row("dev-lab-host", "machine"),
    )
    assert overlay_writers(text, policy, PATTERN) == {"dev-lab-host"}


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("not yaml", "principals: [\n"),
        ("not a mapping", "- a\n"),
        ("no rows", _overlay()),
        ("wrong schema", _overlay(_row("dev-a", "machine")).replace(".v1", ".v2")),
        ("duplicate", _overlay(_row("dev-a", "machine"), _row("dev-a"))),
        ("bad name", _overlay(_row("Dev_A", "machine"))),
        ("row without principal", _overlay({"login": "x", "device": "y"})),
    ],
)
def test_a_malformed_overlay_is_refused(label: str, text: str) -> None:
    with pytest.raises(ValueError, match=r"^overlay"):
        overlay_writers(text, load_writer_overlay_policy(), PATTERN)


class _Bus(EventBusInmemory):
    def __init__(self) -> None:
        super().__init__(environment="local", group="writer-overlay-test")
        self.subscribed: list[str] = []

    async def subscribe(
        self,
        topic: str,
        on_message: Callable[[Any], Awaitable[None]],
        group_id: str,
    ) -> Callable[[], Awaitable[None]]:
        self.subscribed.append(topic)
        return await super().subscribe(topic, on_message=on_message, group_id=group_id)


class _Ledger:
    def __init__(self) -> None:
        self.rows = ""

    def read_text(self) -> str:
        return self.rows

    def append(self, rows: str) -> ModelAppendCommandResult:
        self.rows += rows + "\n"
        return ModelAppendCommandResult(exit_code=0)


def _request() -> ModelWorkLedgerAppendRequest:
    request_id = uuid4()
    return ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=f"2026-10-30T23:00:00Z | STATUS | lane=h051 | req={request_id} | text",
        requested_by_lane="h051",
        requesting_host="h051",
        requested_at=datetime.now(UTC),
    )


def _touch(path: Path, text: str) -> None:
    """Write and move the mtime forward, so a same-second rewrite is still a change."""
    path.write_text(text)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


def test_a_declared_machine_is_served_and_attributed_without_a_reinstall(
    tmp_path: Path,
) -> None:
    """The planted case: h051's login appends only once its machine row is declared."""

    async def scenario() -> None:
        policy = load_writer_overlay_policy()
        overlay = tmp_path / policy.path
        overlay.parent.mkdir(parents=True)
        _touch(overlay, _overlay(_row("dev-person-laptop")))
        bus = _Bus()
        await bus.start()
        ledger = _Ledger()
        handler = HandlerWorkLedgerAppendEffect(
            ledger, ledger, "ledger", writer_principals=["dev-installed"]
        )
        host = WorkLedgerAppendHost(bus, handler, writer_principals=["dev-installed"])

        async def serve(writers: frozenset[str]) -> bool:
            handler.writers.replace(writers)
            return await host.set_writer_principals(writers)

        watch = WriterOverlayWatch(
            overlay,
            policy,
            static_writers=["dev-installed"],
            principal_pattern=PATTERN,
            apply=serve,
        )
        receipts: list[ModelWorkLedgerAppendReceipt] = []

        async def on_receipt(message: ProtocolBusMessage) -> None:
            receipts.append(
                ModelWorkLedgerAppendReceipt.model_validate(
                    json.loads(message.value)["payload"]
                )
            )

        await bus.subscribe(host.topics.success, on_receipt, "receipt-probe")
        assert await watch.refresh() is False  # a developer row is not a writer
        await host.start()
        machine_topic = host.topics.writer_command_topic("dev-h051")

        async def publish() -> None:
            envelope = ModelEventEnvelope[dict[str, object]](
                payload=_request().model_dump(mode="json"),
                event_type=event_type_for(machine_topic),
            )
            await bus.publish(machine_topic, None, envelope.model_dump_json().encode())
            await host.drain()

        try:
            # Before the row: the topic is not consumed and the handler refuses the name.
            assert machine_topic not in bus.subscribed
            await publish()
            assert ledger.rows == ""
            refused = handler.handle(_request(), writer_principal="dev-h051")
            assert refused.status is EnumWorkLedgerAppendStatus.REFUSED
            assert "unknown writer principal dev-h051" in refused.message

            # Declaring the machine row is the one step.
            _touch(
                overlay,
                _overlay(_row("dev-person-laptop"), _row("dev-h051", "machine")),
            )
            assert await watch.refresh() is True
            assert watch.writers == {"dev-installed", "dev-h051"}
            assert machine_topic in host.serving_topics
            assert machine_topic in bus.subscribed
            assert machine_topic in host.serving_topics
            await publish()
            assert [receipt.status for receipt in receipts] == [
                EnumWorkLedgerAppendStatus.ACCEPTED
            ]
            assert receipts[0].principal == "dev-h051"
            assert " | principal=dev-h051 | " in ledger.rows

            # An unchanged file is not re-read; a malformed one keeps the writers.
            assert await watch.refresh() is False
            _touch(overlay, "principals: [\n")
            assert await watch.refresh() is False
            assert machine_topic in host.serving_topics

            # Removing the row stops serving it; the install-time writer stays.
            _touch(overlay, _overlay(_row("dev-person-laptop")))
            assert await watch.refresh() is True
            assert machine_topic not in host.serving_topics
        finally:
            await host.stop()
            await bus.close()

    asyncio.run(scenario())


def test_a_missing_overlay_keeps_the_install_time_writers(tmp_path: Path) -> None:
    async def scenario() -> None:
        applied: list[frozenset[str]] = []

        async def apply(writers: frozenset[str]) -> bool:
            applied.append(writers)
            return True

        watch = WriterOverlayWatch(
            tmp_path / "absent.yaml",
            ModelWriterOverlayPolicy(
                root_env="ROOT",
                path="absent.yaml",
                schema_version="developer_principal.v1",
                default_role="developer",
                roles=("machine",),
                refresh_seconds=1,
            ),
            static_writers=["dev-installed"],
            principal_pattern=PATTERN,
            apply=apply,
        )
        assert await watch.refresh() is False
        assert applied == []
        assert watch.writers == {"dev-installed"}

    asyncio.run(scenario())


def test_the_serve_wiring_reads_the_overlay_under_the_declared_root(
    tmp_path: Path,
) -> None:
    """``follow_overlay_writers`` is what ``onex work-ledger serve`` calls before subscribing."""

    async def scenario() -> None:
        policy = load_writer_overlay_policy()
        overlay = tmp_path / policy.path
        overlay.parent.mkdir(parents=True)
        _touch(overlay, _overlay(_row("dev-h051", "machine")))
        bus = _Bus()
        ledger = _Ledger()
        handler = HandlerWorkLedgerAppendEffect(
            ledger, ledger, "ledger", writer_principals=["dev-installed"]
        )
        host = WorkLedgerAppendHost(bus, handler, writer_principals=["dev-installed"])
        unset = await follow_overlay_writers(host, handler, ["dev-installed"], {})
        await unset.stop()
        assert unset.writers == {"dev-installed"}
        watch = await follow_overlay_writers(
            host, handler, ["dev-installed"], {policy.root_env: str(tmp_path)}
        )
        try:
            assert watch.writers == {"dev-installed", "dev-h051"}
            assert host.topics.writer_command_topic("dev-h051") in host.serving_topics
            refused = handler.handle(_request(), writer_principal="dev-h051")
            assert "unknown writer principal" not in refused.message
        finally:
            await watch.stop()

    asyncio.run(scenario())
