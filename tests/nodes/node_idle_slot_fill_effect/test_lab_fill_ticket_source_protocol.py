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


def _page_fake(total: int):
    """A Linear answer that serves ``total`` open issues of one viewer, ``first`` at a time."""

    def post(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        start = int(variables["after"] or 0)
        end = min(start + int(variables["first"]), total)
        nodes = [
            {
                "identifier": f"OMN-{n + 1}",
                "title": f"t{n}",
                "state": {"name": "Todo", "type": "unstarted"},
                "assignee": {"id": "viewer-1"},
                "labels": {"nodes": [{"name": "launch"}]},
                "updatedAt": "2026-11-27T00:00:00Z",
            }
            for n in range(start, end)
        ]
        return {
            "viewer": {"id": "viewer-1"},
            "issues": {
                "pageInfo": {
                    "hasNextPage": end < total,
                    "endCursor": str(end),
                },
                "nodes": nodes,
            },
        }

    return post


async def test_lab_fill_ticket_source_protocol_linear_reads_an_operator_with_7040_open_issues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.handlers.lab_fill_ticket_source import LinearTicketService
    from omnimarket.nodes.node_linear_snapshot_effect.handlers import (
        handler_linear_snapshot as snapshot,
    )

    monkeypatch.setattr(snapshot, "http_post", _page_fake(7040))
    service = LinearTicketService()
    # "me" is the signed-in user, who owns every row the read returns
    rows = await service.list_tickets(
        {"assignee": "me", "labels": ["launch"], "status_types": ["unstarted"]}, 8000
    )
    assert len(rows) == 7040


def test_lab_fill_ticket_source_protocol_me_resolves_to_the_signed_in_user_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_linear_snapshot_effect.handlers import (
        handler_linear_snapshot as snapshot,
    )

    monkeypatch.setattr(snapshot, "http_post", _page_fake(3))
    env = _env(tmp_path, "linear")
    path = tmp_path / "ovl/node_lab_fill_selection_compute/overlay.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("u1", "me"))
    operator, issues = read_open_launch_issues(env)
    # the ticket plan's owner test compares each issue's assignee with this id, so "me" must not leak out
    assert operator == "viewer-1"
    assert {i["assignee"] for i in issues} == {"viewer-1"}
