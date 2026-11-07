# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared models of the floor fill nodes (OMN-22931).

The candidates compute, the lines compute, the reducer and the orchestrator
import their models from here and from no sibling node package.
"""

from __future__ import annotations

from omnimarket.models.floor_fill.enum_floor_fill_drop_reason import (
    EnumFloorFillDropReason,
)
from omnimarket.models.floor_fill.enum_floor_fill_entry_state import (
    EnumFloorFillEntryState,
)
from omnimarket.models.floor_fill.enum_floor_fill_lines_entry_state import (
    EnumFloorFillLinesEntryState,
)
from omnimarket.models.floor_fill.enum_floor_fill_lines_level import (
    EnumFloorFillLinesLevel,
)
from omnimarket.models.floor_fill.enum_floor_fill_lines_source import (
    EnumFloorFillLinesSource,
)
from omnimarket.models.floor_fill.enum_floor_fill_lines_start_kind import (
    EnumFloorFillLinesStartKind,
)
from omnimarket.models.floor_fill.enum_floor_fill_lines_verdict import (
    EnumFloorFillLinesVerdict,
)
from omnimarket.models.floor_fill.enum_floor_fill_trigger import (
    EnumFloorFillTrigger,
)
from omnimarket.models.floor_fill.model_floor_fill_candidates import (
    ModelFloorEntryTicketVerdict,
    ModelFloorFillCandidate,
    ModelFloorFillCandidatesRequest,
    ModelFloorFillCandidatesResult,
    ModelFloorFillLine,
    ModelFloorFillNotReady,
    ModelFloorFillStateEntry,
    ModelFloorFillUnticketable,
    ModelFloorRecordRead,
    ModelFloorTaskPayloadVerdict,
    parse_stamp,
)
from omnimarket.models.floor_fill.model_floor_fill_intent import (
    ModelFloorFillIntent,
)
from omnimarket.models.floor_fill.model_floor_fill_lines_request import (
    ModelFloorFillLinesEntry,
    ModelFloorFillLinesLine,
    ModelFloorFillLinesRequest,
    ModelFloorFillLinesStartResult,
    ModelFloorFillLinesUntickable,
    line_of_key,
)
from omnimarket.models.floor_fill.model_floor_fill_lines_result import (
    ModelFloorFillLinesFinding,
    ModelFloorFillLinesResult,
)
from omnimarket.models.floor_fill.model_floor_fill_observation import (
    ModelFloorFillObservation,
    ModelFloorFillReservedChecked,
    ModelFloorFillReserveRequested,
    ModelFloorFillSentChecked,
    ModelFloorFillStartFailed,
    ModelFloorFillTicketStored,
)
from omnimarket.models.floor_fill.model_floor_fill_reduce_input import (
    ModelFloorFillReduceInput,
)
from omnimarket.models.floor_fill.model_floor_fill_reduce_output import (
    ModelFloorFillReduceOutput,
)
from omnimarket.models.floor_fill.model_floor_fill_state import (
    FloorKey,
    ModelFloorFillEntry,
    ModelFloorFillState,
    line_and_task,
)

__all__: list[str] = [
    "EnumFloorFillDropReason",
    "EnumFloorFillEntryState",
    "EnumFloorFillLinesEntryState",
    "EnumFloorFillLinesLevel",
    "EnumFloorFillLinesSource",
    "EnumFloorFillLinesStartKind",
    "EnumFloorFillLinesVerdict",
    "EnumFloorFillTrigger",
    "FloorKey",
    "ModelFloorEntryTicketVerdict",
    "ModelFloorFillCandidate",
    "ModelFloorFillCandidatesRequest",
    "ModelFloorFillCandidatesResult",
    "ModelFloorFillEntry",
    "ModelFloorFillIntent",
    "ModelFloorFillLine",
    "ModelFloorFillLinesEntry",
    "ModelFloorFillLinesFinding",
    "ModelFloorFillLinesLine",
    "ModelFloorFillLinesRequest",
    "ModelFloorFillLinesResult",
    "ModelFloorFillLinesStartResult",
    "ModelFloorFillLinesUntickable",
    "ModelFloorFillNotReady",
    "ModelFloorFillObservation",
    "ModelFloorFillReduceInput",
    "ModelFloorFillReduceOutput",
    "ModelFloorFillReserveRequested",
    "ModelFloorFillReservedChecked",
    "ModelFloorFillSentChecked",
    "ModelFloorFillStartFailed",
    "ModelFloorFillState",
    "ModelFloorFillStateEntry",
    "ModelFloorFillTicketStored",
    "ModelFloorFillUnticketable",
    "ModelFloorRecordRead",
    "ModelFloorTaskPayloadVerdict",
    "line_and_task",
    "line_of_key",
    "parse_stamp",
]
