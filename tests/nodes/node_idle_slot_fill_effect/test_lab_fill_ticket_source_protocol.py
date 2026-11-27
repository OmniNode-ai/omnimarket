# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab fill reads open launch work through ProtocolTicketService; the overlay picks the adapter (OMN-26762)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from omnibase_spi.protocols.services.protocol_ticket_service import (
    ProtocolTicketService,
)

from omnimarket.handlers.lab_fill_ticket_source import (
    InMemoryTicketService,
    read_open_launch_issues,
    resolve_ticket_service,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_idle_slot_fill import (
    default_sources,
)

TICKETS: list[dict[str, Any]] = [
    {
        "id": "OMN-1",
        "title": "open one",
        "status": "In Progress",
        "statusType": "started",
        "assignee": "u1",
        "labels": ["launch"],
    },
    {
        "id": "OMN-2",
        "title": "closed one",
        "status": "Done",
        "statusType": "completed",
        "assignee": "u1",
        "labels": ["launch"],
    },
    {
        "id": "OMN-3",
        "title": "not launch",
        "status": "Todo",
        "statusType": "unstarted",
        "assignee": "u1",
        "labels": ["other"],
    },
]

OVERLAY = """\
companion_repositories: []
not_work_repositories: []
document_repositories: []
ticket_source:
  adapter: {adapter}
  assignee: u1
  labels: [launch]
  status_types: [unstarted, started, backlog]
  limit: 50
"""


def _env(tmp_path: Path, adapter: str) -> dict[str, str]:
    root = tmp_path / "ovl"
    (root / "node_lab_fill_selection_compute").mkdir(parents=True)
    (root / "node_lab_fill_selection_compute" / "overlay.yaml").write_text(
        OVERLAY.format(adapter=adapter), encoding="utf-8"
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root), "OMNI_HOME": str(tmp_path)}


def test_lab_fill_ticket_source_protocol_in_memory_adapter_swaps_by_overlay(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path, "in_memory")
    registry = {"in_memory": lambda _cfg: InMemoryTicketService(TICKETS)}
    service = resolve_ticket_service(env, registry)
    assert isinstance(service, ProtocolTicketService)
    operator, issues = read_open_launch_issues(env, registry)
    assert operator == "u1"
    assert [i["id"] for i in issues] == ["OMN-1"]


def test_lab_fill_ticket_source_protocol_linear_adapter_is_the_declared_default(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path, "linear")
    service = resolve_ticket_service(env)
    assert isinstance(service, ProtocolTicketService)
    assert type(service).__name__ == "LinearTicketService"


def test_lab_fill_ticket_source_protocol_unknown_adapter_refuses_naming_it(
    tmp_path: Path,
) -> None:
    with pytest.raises(NodeOverlayError, match="nonesuch"):
        resolve_ticket_service(_env(tmp_path, "nonesuch"))


def test_lab_fill_ticket_source_protocol_no_ticket_source_key_refuses(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path, "in_memory")
    path = tmp_path / "ovl/node_lab_fill_selection_compute/overlay.yaml"
    path.write_text(
        "companion_repositories: []\nnot_work_repositories: []\ndocument_repositories: []\n",
        encoding="utf-8",
    )
    with pytest.raises(NodeOverlayError, match="ticket_source"):
        resolve_ticket_service(env)


def test_lab_fill_ticket_source_protocol_default_sources_reads_no_snapshot_file(
    tmp_path: Path,
) -> None:
    sources = default_sources("2026-11-27T22:00:00Z", _env(tmp_path, "linear"))
    assert sources is not None
    assert sources.linear_snapshot is None
    assert sources.tickets is not None
