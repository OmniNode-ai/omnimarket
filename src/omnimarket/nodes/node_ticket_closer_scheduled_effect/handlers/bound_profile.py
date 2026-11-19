# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bind the real adapters into the scheduled ticket closer (OMN-24301)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from omnimarket.handlers.scm.handler_gateway_read_client import (
    HandlerGatewayReadClient,
)
from omnimarket.models.ticket_closer import ModelTicketCloserScheduleOverlay
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_contract_index import (
    GitCloneContractIndex,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_scope_overlay import (
    load_scope_overlay,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.closer_protocols import (
    ProtocolCloserCandidateSource,
    ProtocolCloserVerifier,
    ProtocolTicketDoneWriter,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.contract_candidate_source import (
    ContractCandidateSource,
    GitCloneMergeReader,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.dod_verify_reading import (
    DodVerifyReader,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.gateway_open_prs import (
    GatewayOpenPullRequests,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.ticket_bus_call import (
    bus_ticket_call,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.ticket_done_writer import (
    BusTicketDoneWriter,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.ticket_reader import (
    BusTicketReader,
)


class BoundProfileRefusedError(Exception):
    """The bound profile is missing a value the real adapters require."""


@dataclass(frozen=True)
class CloserParts:
    """The three real adapters the scheduled closer drives when a bound profile is named."""

    source: ProtocolCloserCandidateSource
    verifier: ProtocolCloserVerifier
    writer: ProtocolTicketDoneWriter


def bound_closer_parts(
    schedule: ModelTicketCloserScheduleOverlay,
    environ: Mapping[str, str],
) -> CloserParts:
    """Build the real adapters from the schedule overlay, or refuse naming the missing thing."""
    if schedule.home_env is None:
        raise BoundProfileRefusedError("home_env is not set in the schedule overlay")
    home = environ.get(schedule.home_env, "")
    if not home:
        raise BoundProfileRefusedError(
            f"environment variable {schedule.home_env} is unset or empty"
        )
    if schedule.bus_lane is None:
        raise BoundProfileRefusedError("bus_lane is not set in the schedule overlay")
    if schedule.ticket_timeout_seconds is None:
        raise BoundProfileRefusedError(
            "ticket_timeout_seconds is not set in the schedule overlay"
        )
    if schedule.scan_limit is None:
        raise BoundProfileRefusedError("scan_limit is not set in the schedule overlay")
    try:
        scope = load_scope_overlay()
    except Exception as exc:
        raise BoundProfileRefusedError(
            f"scope overlay could not be loaded: {type(exc).__name__}"
        ) from exc
    root = Path(home)
    if not any(
        (root / repository.name).is_dir() and (root / repository.name / ".git").exists()
        for repository in scope.pr_repositories
    ):
        raise BoundProfileRefusedError(
            f"no clone of any scope repository under {schedule.home_env}"
        )
    call = bus_ticket_call(
        bus_lane=schedule.bus_lane,
        timeout_s=schedule.ticket_timeout_seconds,
    )
    source = ContractCandidateSource(
        contracts=GitCloneContractIndex(root, scope),
        merges=GitCloneMergeReader(root, scope),
        tickets=BusTicketReader(call, schedule.lane),
        open_prs=GatewayOpenPullRequests(HandlerGatewayReadClient(), scope),
        eligible_states=frozenset(scope.eligible_states),
        scan_limit=schedule.scan_limit,
    )
    verifier = DodVerifyReader()
    writer = BusTicketDoneWriter(call)
    return CloserParts(source=source, verifier=verifier, writer=writer)
