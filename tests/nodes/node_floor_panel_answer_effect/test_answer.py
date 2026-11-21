# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The answer itself: the held row's state against the budget, and every named refusal (OMN-19998).

Every check is a ``problems_`` function returning a list of problems (empty = pass) over a builder
of the handler, so each control can inject one named break as a subclass and show the check fails.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
import yaml
from omnibase_infra.runtime.state_io.state_store_adapter import (
    CONTEXTVAR_STATE_IO_ROWS,
)
from pydantic import BaseModel, ValidationError, create_model

from omnimarket.models.floor_panel.enum_floor_panel_code import EnumFloorPanelCode
from omnimarket.models.floor_panel.model_floor_panel_answer import ModelFloorPanelAnswer
from omnimarket.models.floor_panel.model_floor_panel_command import (
    ModelFloorPanelCommand,
)
from omnimarket.models.floor_panel.model_floor_panel_request import (
    ModelFloorPanelRequest,
)
from omnimarket.models.work_state_panel.enum_panel_refusal import EnumPanelRefusal
from omnimarket.models.work_state_panel.enum_panel_state import EnumPanelState
from omnimarket.models.work_state_panel.model_work_state_panel_row import (
    ModelWorkStatePanelRow,
)
from omnimarket.models.work_state_projection.model_work_state_projected_event import (
    EnumWorkStateProjectorProfile,
    ModelWorkStateProjectedEvent,
)
from omnimarket.nodes.node_floor_panel_answer_effect.handlers.handler_floor_panel_answer import (
    HandlerFloorPanelAnswer,
)
from omnimarket.nodes.node_floor_panel_answer_effect.protocols.adapter_floor_panel_row_reader_memory import (
    InMemoryFloorPanelRowReader,
)
from omnimarket.nodes.node_floor_panel_answer_effect.protocols.adapter_floor_panel_row_reader_memory import (
    RaisingFloorPanelRowReader as RaisingReader,
)
from omnimarket.nodes.node_work_state_panel_reducer.handlers.handler_work_state_panel_reducer import (
    HandlerWorkStatePanelReducer,
)
from omnimarket.nodes.node_work_state_panel_reducer.state_codec import (
    StateIoCodec,
    decode,
    reset_default_proxy,
)
from tests.nodes.node_floor_panel_answer_effect.support import (
    ADDRESS,
    CONTRACT,
    MARKER,
    PANEL_ID,
    PROJECTOR_KEY,
    T0,
    CountingReader,
    HandlerBuild,
    body,
    build_with,
    command,
    handler,
    held_row,
    overlay,
    overlayless_handler,
    reader_with,
    stored,
)

pytestmark = pytest.mark.unit

REFUSED_UNDECODABLE = "refused-" + "2" * 64
REFUSED_FUTURE = "refused-" + "1" * 64


async def problems_panel_states(build: HandlerBuild = handler) -> list[str]:
    """The held row answered by its age against the entry's budget; nothing held, by its refusal."""
    problems: list[str] = []
    for version in (3, 7):
        answer = await build(reader_with(held_row(T0), version), T0).handle(
            command(ADDRESS)
        )
        if answer.row_version != version:
            problems.append(
                f"row_version {answer.row_version} is not the stored version {version}"
            )
    fresh = await build(
        reader_with(held_row(T0), 3), T0 + timedelta(seconds=899)
    ).handle(command(ADDRESS))
    if fresh.state is not EnumPanelState.READ:
        problems.append(f"a held row 899s into a 900s budget answered {fresh.state}")
    if fresh.age_s != 899:
        problems.append(f"a row held at T0 aged {fresh.age_s}s at T0+899s")
    if fresh.as_of != T0 or fresh.body != body(T0):
        problems.append("the fresh answer did not carry the row's as_of and body")
    if fresh.max_age_s != 900:
        problems.append(f"the answer carries a max_age_s of {fresh.max_age_s}")
    stale = await build(
        reader_with(held_row(T0), 3), T0 + timedelta(seconds=901)
    ).handle(command(ADDRESS))
    if stale.state is not EnumPanelState.STALE:
        problems.append(f"a held row 901s into a 900s budget answered {stale.state}")
    if stale.age_s != 901:
        problems.append(f"a row held at T0 aged {stale.age_s}s at T0+901s")
    if stale.as_of != T0 or stale.body != body(T0):
        problems.append("the stale answer lost the row's as_of or body")
    nothing = await build(InMemoryFloorPanelRowReader(), T0).handle(command(ADDRESS))
    if nothing.state is not EnumPanelState.UNREAD or nothing.reason != "not_projected":
        problems.append(
            f"an empty store answered {nothing.state} with reason {nothing.reason!r}"
        )
    if nothing.refusal is not None:
        problems.append("an empty store produced a refusal view")
    empty_row = ModelWorkStatePanelRow(
        last_refusal_code=EnumPanelRefusal.UNDECODABLE,
        last_refusal_at=T0,
        last_refusal_id=REFUSED_UNDECODABLE,
    )
    refused = await build(reader_with(empty_row, 4), T0).handle(command(ADDRESS))
    if refused.state is not EnumPanelState.UNREAD or refused.reason != "undecodable":
        problems.append(
            f"an EMPTY row with an undecodable refusal answered {refused.state}"
            f" with reason {refused.reason!r}"
        )
    if (
        refused.refusal is None
        or refused.refusal.code != "undecodable"
        or refused.refusal.at != T0
        or refused.refusal.refusal_id != REFUSED_UNDECODABLE
    ):
        problems.append("the UNREAD answer did not carry the row's refusal view")
    held_and_refused = held_row(T0, epoch=2).model_copy(
        update={
            "last_refusal_code": EnumPanelRefusal.FUTURE_DATED,
            "last_refusal_at": T0 + timedelta(seconds=5),
            "last_refusal_id": REFUSED_FUTURE,
        }
    )
    beside = await build(
        reader_with(held_and_refused, 3), T0 + timedelta(seconds=10)
    ).handle(command(ADDRESS))
    if beside.state is not EnumPanelState.READ:
        problems.append(
            f"a fresh held row that also records a refusal answered {beside.state}"
        )
    if beside.epoch != 2:
        problems.append(f"a row at epoch 2 answered at epoch {beside.epoch}")
    if (
        beside.refusal is None
        or beside.refusal.code != "future_dated"
        or beside.refusal.refusal_id != REFUSED_FUTURE
    ):
        problems.append("a held row's later refusal was not answered beside its state")
    before = await build(
        reader_with(held_row(T0), 3), T0 - timedelta(seconds=60)
    ).handle(command(ADDRESS))
    if before.age_s != 0:
        problems.append(f"a clock before the row's as_of aged it {before.age_s}s")
    return problems


class _AlwaysReadAnswer(HandlerFloorPanelAnswer):
    """The injected break: every answer says READ, whatever the row or the budget says."""

    async def handle(self, request: ModelFloorPanelCommand) -> ModelFloorPanelAnswer:
        answer = await super().handle(request)
        return answer.model_copy(update={"state": EnumPanelState.READ})


class _ConstantRowVersionAnswer(HandlerFloorPanelAnswer):
    """The injected break: the answer names one row version, never the store's."""

    async def handle(self, request: ModelFloorPanelCommand) -> ModelFloorPanelAnswer:
        answer = await super().handle(request)
        return answer.model_copy(update={"row_version": 1})


async def test_panel_states() -> None:
    assert await problems_panel_states() == []


async def test_control_panel_states_ignores_the_bound() -> None:
    assert await problems_panel_states(build_with(_AlwaysReadAnswer)) != []


async def test_control_panel_states_constant_row_version() -> None:
    assert await problems_panel_states(build_with(_ConstantRowVersionAnswer)) != []


async def problems_request_validation(
    build: HandlerBuild = handler,
    caplog: pytest.LogCaptureFixture | None = None,
) -> list[str]:
    """A payload that does not validate is refused with one fixed reason; the reader never runs."""
    problems: list[str] = []
    counting = CountingReader()
    reasons: set[str | None] = set()
    for payload in ({MARKER: MARKER}, {"panel": "x", "extra": 1}, {}):
        answer = await build(counting, T0).handle(command(payload))
        reasons.add(answer.reason)
        if answer.code is not EnumFloorPanelCode.REQUEST_INVALID:
            problems.append(f"payload {payload!r} answered code {answer.code}")
        if answer.reason != EnumFloorPanelCode.REQUEST_INVALID.value:
            problems.append(
                f"payload {payload!r} refused with reason {answer.reason!r}"
            )
        if (
            answer.panel is not None
            or answer.state is not None
            or answer.body is not None
        ):
            problems.append(f"payload {payload!r} was answered as if it held a panel")
        if MARKER in answer.model_dump_json():
            problems.append("the marker reached the refusal")
    if reasons != {EnumFloorPanelCode.REQUEST_INVALID.value}:
        problems.append(
            f"the refusal reason is not one fixed text: {sorted(map(str, reasons))}"
        )
    if counting.reads != 0:
        problems.append(
            f"the row reader was called {counting.reads} times for invalid requests"
        )
    if caplog is not None and MARKER in caplog.text:
        problems.append("the marker reached the log")
    return problems


class _FieldNameLeakingAnswer(HandlerFloorPanelAnswer):
    """The injected break: the refusal's reason lists the rejected fields by name."""

    async def handle(self, request: ModelFloorPanelCommand) -> ModelFloorPanelAnswer:
        try:
            ModelFloorPanelRequest.model_validate(request.payload)
        except ValidationError as exc:
            names = ",".join(sorted({str(error["loc"][0]) for error in exc.errors()}))
            refusal = await super().handle(request)
            return refusal.model_copy(update={"reason": f"{refusal.reason}:{names}"})
        return await super().handle(request)


async def test_request_validation_mapping(caplog: pytest.LogCaptureFixture) -> None:
    assert await problems_request_validation(caplog=caplog) == []


async def test_control_request_validation_lists_rejected_field_names() -> None:
    assert await problems_request_validation(build_with(_FieldNameLeakingAnswer)) != []


async def problems_panel_field_never_echoes(
    build: HandlerBuild = handler,
    caplog: pytest.LogCaptureFixture | None = None,
) -> list[str]:
    """The panel field names the overlay's own id or nothing — never the request's text."""
    problems: list[str] = []
    unknown = await build(InMemoryFloorPanelRowReader(), T0).handle(
        command({"panel": MARKER})
    )
    if unknown.code is not EnumFloorPanelCode.UNKNOWN:
        problems.append(f"an undeclared panel answered code {unknown.code}")
    if unknown.panel is not None:
        problems.append("an undeclared panel was echoed in the answer's panel field")
    if MARKER in unknown.model_dump_json():
        problems.append("the marker reached the refusal of the undeclared panel")
    invalid = await build(InMemoryFloorPanelRowReader(), T0).handle(
        command({MARKER: MARKER})
    )
    if invalid.panel is not None:
        problems.append("an invalid payload was echoed in the answer's panel field")
    store_down = await build(CountingReader(RaisingReader()), T0).handle(
        command(ADDRESS)
    )
    if store_down.code is not EnumFloorPanelCode.STORE_UNAVAILABLE:
        problems.append(f"a reader that raises answered code {store_down.code}")
    if store_down.panel != PANEL_ID:
        problems.append(
            f"the store refusal named panel {store_down.panel!r}, not the overlay's own id"
        )
    answered = await build(reader_with(held_row(T0), 3), T0).handle(command(ADDRESS))
    if answered.panel != PANEL_ID:
        problems.append(f"a valid request was answered as panel {answered.panel!r}")
    for dump in (
        unknown.model_dump_json(),
        invalid.model_dump_json(),
        store_down.model_dump_json(),
        answered.model_dump_json(),
    ):
        if MARKER in dump:
            problems.append("the marker reached an answer")
            break
    if caplog is not None and MARKER in caplog.text:
        problems.append("the marker reached the log")
    return problems


class _PanelEchoingAnswer(HandlerFloorPanelAnswer):
    """The injected break: the answer echoes the request's own panel text back."""

    async def handle(self, request: ModelFloorPanelCommand) -> ModelFloorPanelAnswer:
        answer = await super().handle(request)
        if answer.panel is None and isinstance(request.payload.get("panel"), str):
            return answer.model_copy(update={"panel": str(request.payload["panel"])})
        return answer


async def test_panel_field_never_echoes(caplog: pytest.LogCaptureFixture) -> None:
    assert await problems_panel_field_never_echoes(caplog=caplog) == []


async def test_control_panel_field_never_echoes_the_request_text() -> None:
    assert (
        await problems_panel_field_never_echoes(build_with(_PanelEchoingAnswer)) != []
    )


async def problems_named_refusals(
    build: HandlerBuild = handler,
    overlayless: HandlerBuild = overlayless_handler,
) -> list[str]:
    """Every way a request cannot be answered names its own code, reason and correlation id."""
    problems: list[str] = []
    unknown = await build(InMemoryFloorPanelRowReader(), T0).handle(
        command({"panel": MARKER})
    )
    entry_invalid = await overlayless(InMemoryFloorPanelRowReader(), T0).handle(
        command(ADDRESS)
    )
    store_unavailable = await build(RaisingReader(), T0).handle(command(ADDRESS))
    for name, expected, answer, expected_panel in (
        ("an unknown panel", EnumFloorPanelCode.UNKNOWN, unknown, None),
        ("no overlay installed", EnumFloorPanelCode.ENTRY_INVALID, entry_invalid, None),
        (
            "a reader that raises",
            EnumFloorPanelCode.STORE_UNAVAILABLE,
            store_unavailable,
            PANEL_ID,
        ),
    ):
        if answer.code is not expected:
            problems.append(f"{name}: code {answer.code} is not {expected}")
        if answer.reason != expected.value:
            problems.append(
                f"{name}: reason {answer.reason!r} is not {expected.value!r}"
            )
        if answer.correlation_id != "c-1":
            problems.append(
                f"{name}: correlation_id {answer.correlation_id!r} was lost"
            )
        if answer.state is not None or answer.body is not None:
            problems.append(f"{name}: the refusal carried a state or a body")
        if answer.panel != expected_panel:
            problems.append(f"{name}: panel {answer.panel!r} is not {expected_panel!r}")
    return problems


class _OneCodeForEverythingAnswer(HandlerFloorPanelAnswer):
    """The injected break: every refusal is flattened onto the one code of the unknown panel."""

    async def handle(self, request: ModelFloorPanelCommand) -> ModelFloorPanelAnswer:
        answer = await super().handle(request)
        if answer.code is None or answer.code is EnumFloorPanelCode.UNKNOWN:
            return answer
        return answer.model_copy(
            update={
                "code": EnumFloorPanelCode.UNKNOWN,
                "reason": EnumFloorPanelCode.UNKNOWN.value,
            }
        )


async def test_named_refusals() -> None:
    assert await problems_named_refusals() == []


async def test_control_named_refusals_flatten_the_names() -> None:
    assert (
        await problems_named_refusals(
            build_with(_OneCodeForEverythingAnswer),
            build_with(_OneCodeForEverythingAnswer, overlayless=True),
        )
        != []
    )


async def problems_answer_reads_runtime_written_row(
    rename: str | None = None,
) -> list[str]:
    """The row the REAL reducer folded through the state_io seam is the row the answer reads (SL4)."""
    problems: list[str] = []
    event = ModelWorkStateProjectedEvent(
        computed_at=T0,
        tick_id="tick-fixture",
        projector=EnumWorkStateProjectorProfile.FILE_SOURCES,
        as_of=T0,
        item_count=3,
        counts={"available": 2, "done": 1},
        ready_in_scope=1,
        unread=("linear",),
        stale_sources=(),
    )
    reset_default_proxy()
    token = CONTEXTVAR_STATE_IO_ROWS.set({PROJECTOR_KEY: (None, 0)})
    try:
        HandlerWorkStatePanelReducer(clock=lambda: T0, overlay=overlay()).handle(event)
        payload = StateIoCodec().flush(PROJECTOR_KEY)
    finally:
        CONTEXTVAR_STATE_IO_ROWS.reset(token)
        reset_default_proxy()
    if payload is None:
        return ["the reducer's dispatch left nothing in the state_io row"]
    document = json.loads(payload)
    for key in ("state", "in_flight", "tenant_id"):
        document.pop(key, None)
    if rename is not None:
        document[rename] = document.pop("epoch")
    try:
        row = decode(json.dumps(document))
    except ValidationError:
        return [
            "the payload the runtime wrote no longer validates as a panel row"
            + (f" once {rename!r} replaced a field" if rename else "")
        ]
    reader = InMemoryFloorPanelRowReader()
    reader.put(PROJECTOR_KEY, stored(row, version=1))
    answer = await handler(reader, T0).handle(command(ADDRESS))
    if answer.state is not EnumPanelState.READ:
        problems.append(
            f"the runtime-written row answered {answer.state} with reason {answer.reason!r}"
        )
    if answer.publication_id != row.publication_id:
        problems.append("the answer's publication_id is not the folded row's")
    if answer.epoch != 1:
        problems.append(f"the first fold answered at epoch {answer.epoch}")
    if answer.row_version != 1:
        problems.append(
            f"the stored version 1 answered as row_version {answer.row_version}"
        )
    if answer.body != row.body:
        problems.append("the answer's body is not the folded row's body")
    return problems


async def test_answer_reads_runtime_written_row() -> None:
    assert await problems_answer_reads_runtime_written_row() == []


async def test_control_answer_reads_runtime_written_row_renames_a_field() -> None:
    assert await problems_answer_reads_runtime_written_row(rename="generation") != []


def _renamed_model(model: type[BaseModel], old: str, new: str) -> type[Any]:
    """A copy of ``model`` with field ``old`` renamed to ``new``; nothing else changes."""
    fields: dict[str, tuple[Any, Any]] = {}
    for name, field in model.model_fields.items():
        fields[new if name == old else name] = (field.annotation, field.default)
    return create_model(
        f"{model.__name__}Renamed", __config__=model.model_config, **fields
    )


async def problems_panel_command_roundtrip(
    command_model: type[ModelFloorPanelCommand] = ModelFloorPanelCommand,
    answer_model: type[ModelFloorPanelAnswer] = ModelFloorPanelAnswer,
) -> list[str]:
    """The command and the answer cross the wire as themselves, and the contract names them (SL2)."""
    problems: list[str] = []
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    if not isinstance(contract, dict):
        return ["the node's contract.yaml does not parse as a mapping"]
    for block, model in (
        (contract.get("input_model"), command_model),
        (contract.get("output_model"), answer_model),
    ):
        if not isinstance(block, dict) or (
            block.get("name") != model.__name__
            or block.get("module") != model.__module__
        ):
            problems.append(
                f"the contract's {model.__name__} does not point at {model.__module__}"
            )
    sent = command({"panel": PANEL_ID})
    try:
        reloaded_command = command_model.model_validate(sent.model_dump(mode="json"))
    except ValidationError:
        problems.append(
            "the command does not survive a model_dump/model_validate round trip"
        )
    else:
        if reloaded_command != sent:
            problems.append(
                "the command does not survive a model_dump/model_validate round trip"
            )
    answer = await handler(reader_with(held_row(T0), 3), T0).handle(command(ADDRESS))
    try:
        reloaded_answer = answer_model.model_validate(answer.model_dump(mode="json"))
    except ValidationError:
        problems.append(
            "the answer does not survive a model_dump/model_validate round trip"
        )
    else:
        if reloaded_answer != answer:
            problems.append(
                "the answer does not survive a model_dump/model_validate round trip"
            )
    return problems


async def test_panel_command_roundtrip() -> None:
    assert await problems_panel_command_roundtrip() == []


async def test_control_panel_command_roundtrip_renames_a_field() -> None:
    assert (
        await problems_panel_command_roundtrip(
            answer_model=_renamed_model(ModelFloorPanelAnswer, "panel", "panel_id")
        )
        != []
    )
