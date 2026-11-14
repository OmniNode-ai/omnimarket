# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The serve takes a declared service login as a writer from the append node's own overlay (OMN-24330).

The dev runtime's own login has no person login or device, so it cannot be a row of the developer principal
overlay. Its name is declared in ``<root>/node_work_ledger_append_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``, and the serve follows that file as it follows the developer overlay.
Shape followed: tests/unit/work_ledger_bus/test_writer_overlay_watch.py.
"""

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from importlib import resources
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
from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV
from omnimarket.handlers.work_ledger_consume.service_writers import (
    ModelServiceWritersPolicy,
    load_service_writers_policy,
    service_writers,
    service_writers_path,
)
from omnimarket.handlers.work_ledger_consume.writer_overlay_watch import (
    ModelWriterOverlayPolicy,
    WriterOverlayWatch,
    follow_overlay_writers,
    load_writer_overlay_policy,
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
SERVICE = "example-service-login"
SCHEMA = "service_writers.v1"


def _declared(*principals: str, schema: str = SCHEMA) -> str:
    return yaml.safe_dump(
        {
            "schema_version": schema,
            "writers": [{"principal": p, "why": "a service login"} for p in principals],
        }
    )


def _service_file(root: Path) -> Path:
    policy = load_service_writers_policy()
    return root / policy.overlay_id / "overlay.yaml"


def _write(path: Path, text: str) -> None:
    """Write and move the mtime forward, so a same-second rewrite is still a change."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


class _Bus(EventBusInmemory):
    def __init__(self) -> None:
        super().__init__(environment="local", group="service-writers-test")

    async def subscribe(
        self,
        topic: str,
        on_message: Callable[[Any], Awaitable[None]],
        group_id: str,
    ) -> Callable[[], Awaitable[None]]:
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
        rows=f"2026-11-14T01:00:00Z | STATUS | lane=dev-runtime | req={request_id} | text",
        requested_by_lane="dev-runtime",
        requesting_host="dev-runtime",
        requested_at=datetime.now(UTC),
    )


def _serving(
    tmp_path: Path,
) -> tuple[_Bus, _Ledger, HandlerWorkLedgerAppendEffect, WorkLedgerAppendHost]:
    bus = _Bus()
    ledger = _Ledger()
    handler = HandlerWorkLedgerAppendEffect(
        ledger, ledger, "ledger", writer_principals=["dev-installed"]
    )
    host = WorkLedgerAppendHost(bus, handler, writer_principals=["dev-installed"])
    return bus, ledger, handler, host


def test_the_contract_declares_the_service_writers_source() -> None:
    policy = load_service_writers_policy()
    assert policy.overlay_id == "node_work_ledger_append_effect"
    assert policy.roots_env == OVERLAY_ROOTS_ENV
    assert policy.schema_version == SCHEMA


def test_the_example_overlay_parses_under_the_declared_policy() -> None:
    text = (
        resources.files("omnimarket.nodes.node_work_ledger_append_effect")
        .joinpath("overlay_service_writers.example.yaml")
        .read_text()
    )
    assert service_writers(text, load_service_writers_policy(), PATTERN) == {
        "example-service-login"
    }


def test_service_writers_path_follows_the_declared_roots(tmp_path: Path) -> None:
    policy = load_service_writers_policy()
    empty, holding = tmp_path / "empty", tmp_path / "holding"
    empty.mkdir()
    _write(_service_file(holding), _declared(SERVICE))
    roots = os.pathsep.join([str(empty), str(holding)])
    assert service_writers_path(policy, {policy.roots_env: roots}) == _service_file(
        holding
    )
    assert service_writers_path(policy, {}) is None
    other = ModelServiceWritersPolicy(
        overlay_id=policy.overlay_id, roots_env="OTHER_ROOTS", schema_version=SCHEMA
    )
    with pytest.raises(ValueError, match=r"^service writers overlay"):
        service_writers_path(other, {"OTHER_ROOTS": roots})


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("not yaml", "writers: [\n"),
        ("not a mapping", "- a\n"),
        ("no writers", _declared()),
        ("wrong schema", _declared(SERVICE, schema="service_writers.v2")),
        ("duplicate", _declared(SERVICE, SERVICE)),
        ("bad name", _declared("Dev_A")),
        (
            "extra key",
            yaml.safe_dump(
                {
                    "schema_version": SCHEMA,
                    "writers": [{"principal": SERVICE, "why": "x", "login": "y"}],
                }
            ),
        ),
        (
            "missing why",
            yaml.safe_dump(
                {"schema_version": SCHEMA, "writers": [{"principal": SERVICE}]}
            ),
        ),
    ],
)
def test_a_malformed_service_overlay_is_refused(label: str, text: str) -> None:
    with pytest.raises(ValueError, match=r"^service writers overlay"):
        service_writers(text, load_service_writers_policy(), PATTERN)


def test_a_declared_service_login_is_served_and_attributed_without_a_reinstall(
    tmp_path: Path,
) -> None:
    """Golden chain after the declaration, error chain before it, on the in-memory bus."""

    async def scenario() -> None:
        environment = {OVERLAY_ROOTS_ENV: str(tmp_path)}
        service_file = _service_file(tmp_path)
        bus, ledger, handler, host = _serving(tmp_path)
        await bus.start()
        watch = await follow_overlay_writers(
            host, handler, ["dev-installed"], environment
        )
        receipts: list[ModelWorkLedgerAppendReceipt] = []

        async def on_receipt(message: ProtocolBusMessage) -> None:
            receipts.append(
                ModelWorkLedgerAppendReceipt.model_validate(
                    json.loads(message.value)["payload"]
                )
            )

        await bus.subscribe(host.topics.success, on_receipt, "receipt-probe")
        await host.start()
        service_topic = host.topics.writer_command_topic(SERVICE)

        async def publish() -> None:
            envelope = ModelEventEnvelope[dict[str, object]](
                payload=_request().model_dump(mode="json"),
                event_type=event_type_for(service_topic),
            )
            await bus.publish(service_topic, None, envelope.model_dump_json().encode())
            await host.drain()

        try:
            # ERROR chain: before the declaration the login's rows are not taken and the name is refused.
            await publish()
            assert ledger.rows == ""
            refused = handler.handle(_request(), writer_principal=SERVICE)
            assert refused.status is EnumWorkLedgerAppendStatus.REFUSED
            assert f"unknown writer principal {SERVICE}" in refused.message

            # GOLDEN chain: declaring the login is the one step.
            _write(service_file, _declared(SERVICE))
            assert await watch.refresh() is True
            assert watch.writers == {"dev-installed", SERVICE}
            assert service_topic in host.serving_topics
            await publish()
            assert [receipt.status for receipt in receipts] == [
                EnumWorkLedgerAppendStatus.ACCEPTED
            ]
            assert receipts[0].principal == SERVICE
            assert f" | principal={SERVICE} | " in ledger.rows

            # A malformed rewrite and a deleted file keep the writer served.
            _write(service_file, "writers: [\n")
            assert await watch.refresh() is False
            assert service_topic in host.serving_topics
            service_file.unlink()
            assert await watch.refresh() is False
            assert service_topic in host.serving_topics

            # Declaring another login in its place stops serving the first.
            _write(service_file, _declared("other-service"))
            assert await watch.refresh() is True
            assert service_topic not in host.serving_topics
        finally:
            await watch.stop()
            await host.stop()
            await bus.close()

    asyncio.run(scenario())


def test_service_and_machine_sources_are_unioned(tmp_path: Path) -> None:
    async def scenario() -> None:
        developer = load_writer_overlay_policy()
        machine_file = tmp_path / "config" / developer.path
        _write(
            machine_file,
            yaml.safe_dump(
                {
                    "schema_version": developer.schema_version,
                    "principals": [
                        {
                            "principal": "dev-h051",
                            "login": "someone@example.com",
                            "device": "box",
                            "role": "machine",
                        }
                    ],
                }
            ),
        )
        service_file = _service_file(tmp_path)
        _write(service_file, _declared(SERVICE))
        _, _, handler, host = _serving(tmp_path)
        environment = {
            OVERLAY_ROOTS_ENV: str(tmp_path),
            developer.root_env: str(tmp_path / "config"),
        }
        watch = await follow_overlay_writers(
            host, handler, ["dev-installed"], environment
        )
        try:
            assert watch.writers == {"dev-installed", "dev-h051", SERVICE}
            _write(service_file, _declared("other-service"))
            assert await watch.refresh() is True
            assert watch.writers == {"dev-installed", "dev-h051", "other-service"}
        finally:
            await watch.stop()

    asyncio.run(scenario())


def test_without_the_service_source_the_login_is_not_served(tmp_path: Path) -> None:
    """Failing control: the watch as it was wired before the change never serves the login."""

    async def scenario() -> None:
        service_file = _service_file(tmp_path)
        _write(service_file, _declared(SERVICE))
        applied: list[frozenset[str]] = []

        async def apply(writers: frozenset[str]) -> bool:
            applied.append(writers)
            return True

        watch = WriterOverlayWatch(
            None,
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
        # The file does name the login: the missing wiring is the only difference.
        text = service_file.read_text()
        assert SERVICE in service_writers(text, load_service_writers_policy(), PATTERN)

    asyncio.run(scenario())
