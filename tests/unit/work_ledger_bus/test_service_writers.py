# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The serve takes a service login as a writer from the append node's overlay (OMN-24357).

The dev runtime's own login is neither a developer nor a machine row of the principal overlay, so
its rows were never consumed. The append node's own overlay declares it as ``service_writers``.
"""

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
from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV
from omnimarket.handlers.work_ledger_consume.writer_overlay_watch import (
    WriterOverlayWatch,
    follow_overlay_writers,
    load_service_writers_policy,
    load_writer_overlay_policy,
    service_overlay_path,
    service_writers_of,
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
SERVICE = load_service_writers_policy()
CONFIG_ROOT_ENV = load_writer_overlay_policy().root_env
LOGIN = "dev-service-login"


def _overlay(*logins: str) -> str:
    return yaml.safe_dump({"service_writers": list(logins)})


def _touch(path: Path, text: str) -> None:
    """Write and move the mtime forward, so a same-second rewrite is still a change."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


def _service_file(root: Path) -> Path:
    return root / SERVICE.overlay_id / "overlay.yaml"


class _Bus(EventBusInmemory):
    def __init__(self) -> None:
        super().__init__(environment="local", group="service-writers-test")
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
        rows=f"2026-11-14T08:00:00Z | STATUS | lane=dev-runtime | req={request_id} | text",
        requested_by_lane="dev-runtime",
        requesting_host="dev-runtime",
        requested_at=datetime.now(UTC),
    )


def _watch(
    handler: HandlerWorkLedgerAppendEffect,
    host: WorkLedgerAppendHost,
    locate: Callable[[], Path | None],
    *,
    static: tuple[str, ...] = ("dev-installed",),
) -> WriterOverlayWatch:
    async def serve(writers: frozenset[str]) -> bool:
        handler.writers.replace(writers)
        return await host.set_writer_principals(writers)

    return WriterOverlayWatch(
        None,
        load_writer_overlay_policy(),
        static_writers=static,
        principal_pattern=PATTERN,
        apply=serve,
        service_policy=SERVICE,
        service_locator=locate,
    )


def test_the_contract_declares_where_the_service_overlay_lives() -> None:
    assert SERVICE.overlay_id == "node_work_ledger_append_effect"
    assert service_overlay_path(SERVICE, CONFIG_ROOT_ENV, {}) is None


def test_declared_service_login_is_served_and_attributed_without_a_reinstall(
    tmp_path: Path,
) -> None:
    """The login appends only once its overlay declares it; the row is attributed to it."""

    async def scenario() -> None:
        root = tmp_path / "roots"
        env = {OVERLAY_ROOTS_ENV: str(root)}
        bus = _Bus()
        await bus.start()
        ledger = _Ledger()
        handler = HandlerWorkLedgerAppendEffect(
            ledger, ledger, "ledger", writer_principals=["dev-installed"]
        )
        host = WorkLedgerAppendHost(bus, handler, writer_principals=["dev-installed"])
        watch = _watch(
            handler, host, lambda: service_overlay_path(SERVICE, CONFIG_ROOT_ENV, env)
        )
        receipts: list[ModelWorkLedgerAppendReceipt] = []

        async def on_receipt(message: ProtocolBusMessage) -> None:
            receipts.append(
                ModelWorkLedgerAppendReceipt.model_validate(
                    json.loads(message.value)["payload"]
                )
            )

        await bus.subscribe(host.topics.success, on_receipt, "receipt-probe")
        assert await watch.refresh() is False  # no overlay yet
        await host.start()
        topic = host.topics.writer_command_topic(LOGIN)

        async def publish() -> None:
            envelope = ModelEventEnvelope[dict[str, object]](
                payload=_request().model_dump(mode="json"),
                event_type=event_type_for(topic),
            )
            await bus.publish(topic, None, envelope.model_dump_json().encode())
            await host.drain()

        try:
            # Before the declaration the topic is not consumed and the handler refuses the name.
            assert topic not in bus.subscribed
            await publish()
            assert ledger.rows == ""
            refused = handler.handle(_request(), writer_principal=LOGIN)
            assert refused.status is EnumWorkLedgerAppendStatus.REFUSED
            assert f"unknown writer principal {LOGIN}" in refused.message

            # Declaring the login in the node's overlay is the one step.
            _touch(_service_file(root), _overlay(LOGIN))
            assert await watch.refresh() is True
            assert watch.writers == {"dev-installed", LOGIN}
            assert topic in host.serving_topics
            await publish()
            assert [receipt.status for receipt in receipts] == [
                EnumWorkLedgerAppendStatus.ACCEPTED
            ]
            assert receipts[0].principal == LOGIN
            assert f" | principal={LOGIN} | " in ledger.rows
            assert await watch.refresh() is False  # an unchanged file is not re-read
        finally:
            await host.stop()
            await bus.close()

    asyncio.run(scenario())


def test_the_service_overlay_falls_back_to_the_clone_beside_the_config_root(
    tmp_path: Path,
) -> None:
    config_root = tmp_path / "workspace"
    config_root.mkdir()
    clone_overlays = tmp_path / SERVICE.clone_dir / SERVICE.clone_subdir
    beside = _service_file(clone_overlays)
    env = {CONFIG_ROOT_ENV: str(config_root)}
    assert service_overlay_path(SERVICE, CONFIG_ROOT_ENV, env) is None
    _touch(beside, _overlay(LOGIN))
    assert service_overlay_path(SERVICE, CONFIG_ROOT_ENV, env) == beside
    # A declared root that holds the overlay wins over the clone beside the config root.
    declared = tmp_path / "declared"
    _touch(_service_file(declared), _overlay("dev-other"))
    both = {**env, OVERLAY_ROOTS_ENV: str(declared)}
    assert service_overlay_path(SERVICE, CONFIG_ROOT_ENV, both) == _service_file(
        declared
    )
    # A declared root without the file does not hide the clone.
    empty = {**env, OVERLAY_ROOTS_ENV: str(tmp_path / "empty")}
    assert service_overlay_path(SERVICE, CONFIG_ROOT_ENV, empty) == beside


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("not yaml", "service_writers: [\n"),
        ("not a mapping", "- a\n"),
        ("no field", "other: [a]\n"),
        ("empty list", _overlay()),
        ("a string", "service_writers: dev-service-login\n"),
        ("duplicate", _overlay(LOGIN, LOGIN)),
        ("bad name", _overlay("Dev_Login")),
    ],
)
def test_a_malformed_service_overlay_is_refused(label: str, text: str) -> None:
    with pytest.raises(ValueError, match=r"^service overlay"):
        service_writers_of(text, PATTERN)


def test_a_malformed_or_deleted_service_overlay_keeps_the_served_writers(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        bus = _Bus()
        ledger = _Ledger()
        handler = HandlerWorkLedgerAppendEffect(
            ledger, ledger, "ledger", writer_principals=["dev-installed"]
        )
        host = WorkLedgerAppendHost(bus, handler, writer_principals=["dev-installed"])
        env = {OVERLAY_ROOTS_ENV: str(tmp_path)}
        path = _service_file(tmp_path)
        watch = _watch(
            handler, host, lambda: service_overlay_path(SERVICE, CONFIG_ROOT_ENV, env)
        )
        _touch(path, _overlay(LOGIN))
        assert await watch.refresh() is True
        assert watch.writers == {"dev-installed", LOGIN}
        _touch(path, "service_writers: [\n")
        assert await watch.refresh() is False
        assert watch.writers == {"dev-installed", LOGIN}
        _touch(path, _overlay("Dev_Login"))
        assert await watch.refresh() is False
        assert watch.writers == {"dev-installed", LOGIN}
        path.unlink()
        assert await watch.refresh() is False
        assert watch.writers == {"dev-installed", LOGIN}
        assert (
            handler.handle(_request(), writer_principal=LOGIN).status
            is not EnumWorkLedgerAppendStatus.REFUSED
        )
        # Rewritten, the file is read again and removing the login stops serving it.
        _touch(path, _overlay("dev-second"))
        assert await watch.refresh() is True
        assert watch.writers == {"dev-installed", "dev-second"}

    asyncio.run(scenario())


def test_service_writers_are_unioned_with_the_install_time_and_machine_writers(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        policy = load_writer_overlay_policy()
        config_root = tmp_path / "workspace"
        principal_overlay = config_root / policy.path
        principal_overlay.parent.mkdir(parents=True)
        _touch(
            principal_overlay,
            yaml.safe_dump(
                {
                    "schema_version": policy.schema_version,
                    "principals": [
                        {"principal": "dev-h051", "role": "machine"},
                        {"principal": "dev-person", "role": "developer"},
                    ],
                }
            ),
        )
        _touch(
            _service_file(tmp_path / SERVICE.clone_dir / SERVICE.clone_subdir),
            _overlay(LOGIN),
        )
        bus = _Bus()
        ledger = _Ledger()
        handler = HandlerWorkLedgerAppendEffect(
            ledger, ledger, "ledger", writer_principals=["dev-installed"]
        )
        host = WorkLedgerAppendHost(bus, handler, writer_principals=["dev-installed"])
        watch = await follow_overlay_writers(
            host, handler, ["dev-installed"], {CONFIG_ROOT_ENV: str(config_root)}
        )
        try:
            assert watch.writers == {"dev-installed", "dev-h051", LOGIN}
            assert watch.service_writers == {LOGIN}
            assert watch.overlay_writers == {"dev-h051"}
            for name in ("dev-installed", "dev-h051", LOGIN):
                assert host.topics.writer_command_topic(name) in host.serving_topics
                refused = handler.handle(_request(), writer_principal=name)
                assert "unknown writer principal" not in refused.message
        finally:
            await watch.stop()

    asyncio.run(scenario())
