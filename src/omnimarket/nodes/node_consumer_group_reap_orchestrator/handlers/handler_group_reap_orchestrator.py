# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Consumer group reap ORCHESTRATOR handler (OMN-23992).

One reducer record in, at most one reap command out. The command is built from the record by
``ModelGroupReapCommand.from_state_record``, so it carries the reducer's decision digest and nothing else decides
what is deleted. A record that names no group emits nothing. The command's id is derived from the record's census,
so the same record always builds the same command and a replayed record cannot start a second reap.
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.models.consumer_group_reap import ModelGroupReapCommand
from omnimarket.models.consumer_group_state import ModelGroupStateRecord

HANDLER_ID = "consumer-group-reap-orchestrator"
REAP_COMMAND_TOPIC = "onex.cmd.omnimarket.consumer-group-reap-requested.v1"


class GroupReapRecordInconsistentError(ValueError):
    """A record that names a due group its own group list does not carry: no command is built from it."""


def command_id(record: ModelGroupStateRecord) -> UUID:
    """The id of the one command a record builds: the same record gives the same id."""
    return uuid5(
        NAMESPACE_URL,
        f"omnimarket:consumer-group-reap:{record.bus_lane}:{record.census_correlation_id}",
    )


def decide(record: ModelGroupStateRecord) -> ModelGroupReapCommand | None:
    """The reap command a record decides, or none when it names no group. Pure."""
    if not record.reap_due:
        return None
    held = {g.name for g in record.groups if g.empty_since is not None}
    missing = sorted(set(record.reap_due) - held)
    if missing:
        raise GroupReapRecordInconsistentError(
            f"bus lane {record.bus_lane}: due groups with no empty_since in the record: {len(missing)}"
        )
    return ModelGroupReapCommand.from_state_record(record, command_id(record))


class HandlerGroupReapOrchestrator:
    """ORCHESTRATOR handler: a reducer record in, the reap command it decides out."""

    def handle(self, request: ModelGroupStateRecord) -> ModelHandlerOutput[None]:
        command = decide(request)
        correlation_id = request.census_correlation_id
        events: tuple[ModelEventEnvelope[ModelGroupReapCommand], ...] = ()
        if command is not None:
            events = (
                ModelEventEnvelope(
                    payload=command,
                    correlation_id=correlation_id,
                    event_type=REAP_COMMAND_TOPIC,
                ),
            )
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=command_id(request),
            correlation_id=correlation_id,
            handler_id=HANDLER_ID,
            events=events,
        )


__all__: list[str] = [
    "HANDLER_ID",
    "REAP_COMMAND_TOPIC",
    "GroupReapRecordInconsistentError",
    "HandlerGroupReapOrchestrator",
    "command_id",
    "decide",
]
