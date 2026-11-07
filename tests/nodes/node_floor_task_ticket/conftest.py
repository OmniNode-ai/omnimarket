# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real-gate tests need the installed ticket-creation guard; where none is installed they are deselected, not skipped.

The create gate is a script of the omniclaude plugin that ``node_create_ticket`` runs as a subprocess; it is not yet an
omnibase_core validator, so a runner without the plugin cannot run it. A test marked ``real_gate`` is removed at
collection, and the reason is printed in the run header, when ``locate_ticket_guard`` finds no guard. On a lab host the
guard is installed and the tests run: ``ONEX_TICKET_GUARD_PATH=<onex plugin dir>/hooks/lib/ticket_creation_guard.py``.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    TicketGuardUnavailableError,
    locate_ticket_guard,
)


def _guard_unavailable() -> str | None:
    try:
        locate_ticket_guard()
    except TicketGuardUnavailableError as error:
        return str(error)
    return None


def pytest_report_header(config: pytest.Config) -> str | None:
    reason = _guard_unavailable()
    if reason is None:
        return "real_gate: the installed ticket-creation guard was found; the real-gate tests run"
    return f"real_gate: no installed ticket-creation guard, the real-gate tests are deselected ({reason})"


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if _guard_unavailable() is None:
        return
    kept = [item for item in items if item.get_closest_marker("real_gate") is None]
    dropped = [
        item for item in items if item.get_closest_marker("real_gate") is not None
    ]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept
