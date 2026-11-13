# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain events of the ledger rows loader settings node (OMN-24059).

Golden chain: a host's row gives the loader command (the registry root put in), the call bound and the
loader overlay's fallback flag, with one ``answered`` event. Error chain: a host with no row is
``host-row-missing``, and a missing or malformed overlay is ``refused`` naming the overlay, each with one
``refused`` event and never a default command. Named break: a handler that returns a default command for
a host with no row fails the no-default check.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.models.model_work_ledger_reader import EnumReaderEventKind
from omnimarket.nodes.node_ledger_rows_loader_effect.models import (
    ModelRowsLoaderOverlay,
)
from omnimarket.nodes.node_ledger_rows_loader_settings_effect.adapters import (
    InMemoryLoaderSettingsSink,
)
from omnimarket.nodes.node_ledger_rows_loader_settings_effect.handlers import (
    HandlerLoaderSettings,
)
from omnimarket.nodes.node_ledger_rows_loader_settings_effect.handlers.handler_loader_settings import (
    load_topics,
)
from omnimarket.nodes.node_ledger_rows_loader_settings_effect.models import (
    EnumLoaderSettingsStatus,
    ModelLoaderHostsOverlay,
    ModelLoaderSettingsRequest,
    ModelLoaderSettingsResult,
)
from tests.unit.nodes.node_ledger_rows_loader_settings_effect.conftest_support import (
    COMMAND,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 13, 3, 0, 0, tzinfo=UTC)
REGISTRY_ROOT = "/registry/root"
HOSTS = ModelLoaderHostsOverlay.model_validate(
    {
        "schema_version": "ledger_rows_loader_hosts.v1",
        "timeout_seconds": 45,
        "hosts": [{"host": "hp-gen8", "command": COMMAND}],
    }
)


def loader_overlay(allowed: bool = True) -> ModelRowsLoaderOverlay:
    return ModelRowsLoaderOverlay(file_fallback_allowed=allowed, journal_env="J")


def build(
    *,
    allowed: bool = True,
    loader_error: Exception | None = None,
    hosts_error: Exception | None = None,
    hosts: ModelLoaderHostsOverlay = HOSTS,
) -> tuple[HandlerLoaderSettings, InMemoryLoaderSettingsSink]:
    def loader_source() -> ModelRowsLoaderOverlay:
        if loader_error is not None:
            raise loader_error
        return loader_overlay(allowed)

    def hosts_source() -> ModelLoaderHostsOverlay:
        if hosts_error is not None:
            raise hosts_error
        return hosts

    sink = InMemoryLoaderSettingsSink()
    return HandlerLoaderSettings(
        loader_source, hosts_source, sink, clock=lambda: NOW
    ), sink


def ask(host: str = "Hp-Gen8") -> ModelLoaderSettingsRequest:
    return ModelLoaderSettingsRequest(registry_root=REGISTRY_ROOT, host=host)


def assert_no_default_command(result: ModelLoaderSettingsResult) -> None:
    """A host with no row, or a refused overlay, never carries a command."""
    if result.status is not EnumLoaderSettingsStatus.RESOLVED:
        assert result.argv is None
        assert result.event.kind is EnumReaderEventKind.REFUSED


@pytest.mark.parametrize("allowed", [True, False])
def test_golden_chain_a_host_row_gives_the_command_the_bound_and_the_flag(
    allowed: bool,
) -> None:
    handler, sink = build(allowed=allowed)
    result = handler.handle(ask())
    assert result.status is EnumLoaderSettingsStatus.RESOLVED
    assert result.argv == (
        f"{REGISTRY_ROOT}/.onex-dispatch-venv/bin/python",
        "-m",
        "omnimarket.nodes.node_ledger_rows_loader_effect",
    )
    assert (result.timeout_s, result.file_fallback_allowed) == (45, allowed)
    assert result.reason == "host-row-found"
    (event,) = sink.events
    assert event == result.event
    assert event.kind is EnumReaderEventKind.ANSWERED
    assert event.topic == load_topics()["loader_settings_resolved"]
    assert event.at == NOW
    assert event.host == "Hp-Gen8"


@pytest.mark.parametrize("allowed", [True, False])
def test_error_chain_a_host_with_no_row_has_no_command_and_one_refused_event(
    allowed: bool,
) -> None:
    handler, sink = build(allowed=allowed)
    result = handler.handle(ask("other-host"))
    assert result.status is EnumLoaderSettingsStatus.HOST_ROW_MISSING
    assert (result.reason, result.argv) == ("loader-host-row-missing", None)
    assert (result.timeout_s, result.file_fallback_allowed) == (45, allowed)
    (event,) = sink.events
    assert event.kind is EnumReaderEventKind.REFUSED
    assert event.topic == load_topics()["loader_settings_refused"]
    assert_no_default_command(result)


def test_error_chain_a_loader_overlay_that_is_missing_is_refused_naming_it() -> None:
    error = NodeOverlayNotFoundError(
        "node_ledger_rows_loader_effect: the overlay file is not configured"
    )
    handler, sink = build(loader_error=error)
    result = handler.handle(ask())
    assert result.status is EnumLoaderSettingsStatus.REFUSED
    assert "node_ledger_rows_loader_effect" in result.reason
    assert (result.file_fallback_allowed, result.argv, result.timeout_s) == (
        None,
        None,
        None,
    )
    assert [e.kind for e in sink.events] == [EnumReaderEventKind.REFUSED]
    assert_no_default_command(result)


def test_error_chain_a_hosts_overlay_that_is_malformed_is_refused_naming_it() -> None:
    error = NodeOverlayError(
        "overlay x is invalid for ledger_rows_loader_hosts (ValidationError): fields ['timeout_seconds']"
    )
    handler, sink = build(hosts_error=error, allowed=False)
    result = handler.handle(ask())
    assert result.status is EnumLoaderSettingsStatus.REFUSED
    assert "ledger_rows_loader_hosts" in result.reason
    assert (result.file_fallback_allowed, result.argv) == (False, None)
    assert [e.kind for e in sink.events] == [EnumReaderEventKind.REFUSED]
    assert_no_default_command(result)


def test_every_handle_emits_exactly_one_event_to_the_sink() -> None:
    handler, sink = build()
    handler.handle(ask())
    handler.handle(ask("nobody"))
    assert [e.status for e in sink.events] == [
        EnumLoaderSettingsStatus.RESOLVED,
        EnumLoaderSettingsStatus.HOST_ROW_MISSING,
    ]


def test_failing_control_a_handler_that_defaults_the_command_fails_the_check() -> None:
    """The named break: a default command for a host with no row is caught by the no-default check."""
    handler, _ = build()
    honest = handler.handle(ask("nobody"))
    assert_no_default_command(honest)  # the real handler passes
    broken = honest.model_copy(update={"argv": ("python", "-m", "default")})
    with pytest.raises(AssertionError):
        assert_no_default_command(broken)


def test_contract_names_the_topics() -> None:
    topics = load_topics()
    assert set(topics) == {"loader_settings_resolved", "loader_settings_refused"}
    assert all(t.startswith("onex.evt.omnimarket.") for t in topics.values())
