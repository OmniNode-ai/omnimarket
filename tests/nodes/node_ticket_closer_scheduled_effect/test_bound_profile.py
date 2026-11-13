# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bound-profile binding and refusal chains of the scheduled ticket closer (OMN-24301)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.models.ticket_closer import (
    EnumCloserReadingSource,
    ModelTicketCloserScheduleOverlay,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_scope_overlay import (
    PACKAGED_OVERLAY,
    load_scope_overlay,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.bound_profile import (
    BoundProfileRefusedError,
    CloserParts,
    bound_closer_parts,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.contract_candidate_source import (
    ContractCandidateSource,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.dod_verify_reading import (
    DodVerifyReader,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.handler_ticket_closer_scheduled import (
    HandlerTicketCloserScheduled,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.ticket_done_writer import (
    BusTicketDoneWriter,
)

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).parent
    / "fixtures/overlays/node_ticket_closer_scheduled_effect/overlay.yaml"
)
T0 = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def schedule() -> ModelTicketCloserScheduleOverlay:
    return ModelTicketCloserScheduleOverlay.model_validate(
        yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    )


def tick(at: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=at,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=at,
        correlation_id=uuid4(),
        scheduler_id="runtime-fixture",
        tick_interval_ms=1000,
    )


def contract_schedule() -> ModelTicketCloserScheduleOverlay:
    return schedule().model_copy(
        update={
            "reading_source": EnumCloserReadingSource.CONTRACT_CLONES,
            "home_env": "TEST_HOME",
            "bus_lane": "dev",
            "ticket_timeout_seconds": 5.0,
            "scan_limit": 3,
        }
    )


def test_bound_profile_golden_builds_the_real_adapters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.bound_profile.load_scope_overlay",
        lambda: load_scope_overlay(PACKAGED_OVERLAY),
    )
    parts = bound_closer_parts(
        contract_schedule(), {"TEST_HOME": str(tmp_path)}, clock=lambda: T0
    )
    assert isinstance(parts, CloserParts)
    assert isinstance(parts.source, ContractCandidateSource)
    assert isinstance(parts.verifier, DodVerifyReader)
    assert isinstance(parts.writer, BusTicketDoneWriter)


@pytest.mark.parametrize(
    ("fragment", "over", "environ"),
    [
        ("home_env", {"home_env": None}, {"TEST_HOME": "/home"}),
        ("TEST_HOME", {}, {}),
        ("bus_lane", {"bus_lane": None}, {"TEST_HOME": "/home"}),
        (
            "ticket_timeout_seconds",
            {"ticket_timeout_seconds": None},
            {"TEST_HOME": "/home"},
        ),
        ("scan_limit", {"scan_limit": None}, {"TEST_HOME": "/home"}),
    ],
)
def test_bound_profile_refuses_naming_the_missing_thing(
    fragment: str,
    over: dict[str, Any],
    environ: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = contract_schedule().model_dump()
    base.update(over)
    sched = ModelTicketCloserScheduleOverlay.model_validate(base)
    monkeypatch.setattr(
        "omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.bound_profile.load_scope_overlay",
        lambda: load_scope_overlay(PACKAGED_OVERLAY),
    )
    with pytest.raises(BoundProfileRefusedError) as excinfo:
        bound_closer_parts(sched, environ, clock=lambda: T0)
    assert fragment in str(excinfo.value)


def test_bound_profile_refuses_when_scope_overlay_cannot_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise() -> Any:
        raise FileNotFoundError("scope overlay missing")

    monkeypatch.setattr(
        "omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.bound_profile.load_scope_overlay",
        _raise,
    )
    with pytest.raises(BoundProfileRefusedError) as excinfo:
        bound_closer_parts(
            contract_schedule(), {"TEST_HOME": str(tmp_path)}, clock=lambda: T0
        )
    assert "scope overlay could not be loaded" in str(excinfo.value)


def test_handler_error_chain_refuses_and_holds_the_interval(
    tmp_path: Path,
) -> None:
    sched = contract_schedule().model_copy(update={"bus_lane": None})
    handler = HandlerTicketCloserScheduled(
        schedule=sched, environ={"TEST_HOME": str(tmp_path)}
    )
    result = handler.handle(tick(T0))
    assert result is not None
    assert result.candidates_read is False
    assert result.flipped == 0
    assert result.examined == 0
    assert result.unread_reason.startswith("bound profile refused")
    assert handler.handle(tick(T0 + timedelta(seconds=10))) is None


def test_handler_unread_reason_when_no_source_is_bound() -> None:
    sched = schedule().model_copy(
        update={"reading_source": EnumCloserReadingSource.UNREAD}
    )
    handler = HandlerTicketCloserScheduled(schedule=sched)
    result = handler.handle(tick(T0))
    assert result is not None
    assert result.candidates_read is False
    assert (
        result.unread_reason == "reading_source is unread: no candidate source is bound"
    )


class _NothingToRead:
    def candidates(self, now: datetime, limit: int) -> None:
        return None


def test_handler_reports_a_source_that_could_not_read() -> None:
    handler = HandlerTicketCloserScheduled(
        schedule=contract_schedule(), source=_NothingToRead()
    )
    result = handler.handle(tick(T0))
    assert result is not None
    assert result.candidates_read is False
    assert (
        result.unread_reason
        == "the candidate source could not read its contracts, merges or pull requests"
    )


def test_failing_control_the_unread_and_bound_readings_differ() -> None:
    unread = schedule().model_copy(
        update={"reading_source": EnumCloserReadingSource.UNREAD}
    )
    bound = contract_schedule().model_copy(update={"bus_lane": None})
    unread_result = HandlerTicketCloserScheduled(schedule=unread).handle(tick(T0))
    bound_result = HandlerTicketCloserScheduled(
        schedule=bound, environ={"TEST_HOME": "/home"}
    ).handle(tick(T0))
    assert unread_result is not None
    assert bound_result is not None
    assert (
        bound_result.unread_reason
        != "reading_source is unread: no candidate source is bound"
    )
    assert (
        unread_result.unread_reason
        == "reading_source is unread: no candidate source is bound"
    )
    assert unread_result.unread_reason != bound_result.unread_reason


class _EmptySource:
    def candidates(self, now: datetime, limit: int) -> list[Any]:
        return []


def test_the_bound_parts_are_built_once_and_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers import (
        handler_ticket_closer_scheduled as handler_module,
    )

    built: list[int] = []

    def fake(schedule: Any, environ: Any, clock: Any) -> CloserParts:
        built.append(1)
        return CloserParts(
            source=_EmptySource(),  # type: ignore[arg-type]
            verifier=DodVerifyReader(),
            writer=BusTicketDoneWriter(lambda _request: None),  # type: ignore[arg-type, return-value]
        )

    monkeypatch.setattr(handler_module, "bound_closer_parts", fake)
    handler = HandlerTicketCloserScheduled(
        schedule=contract_schedule(), environ={"TEST_HOME": str(tmp_path)}
    )
    first = handler.handle(tick(T0))
    later = T0.replace(hour=13)
    second = handler.handle(tick(later))
    assert first is not None
    assert second is not None
    assert first.candidates_read is True
    assert second.candidates_read is True
    assert built == [1]


def test_seam_the_bound_profile_builds_from_the_scope_overlay_the_node_ships(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_scope_overlay import (
        PACKAGED_OVERLAY,
    )

    shipped = load_scope_overlay(PACKAGED_OVERLAY)
    monkeypatch.setattr(
        "omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.bound_profile.load_scope_overlay",
        lambda: shipped,
    )
    parts = bound_closer_parts(
        contract_schedule(), {"TEST_HOME": str(tmp_path)}, clock=lambda: T0
    )
    assert isinstance(parts.source, ContractCandidateSource)


def test_failing_control_a_bound_schedule_never_reads_as_the_unread_profile() -> None:
    bound = contract_schedule().model_copy(update={"bus_lane": None})
    result = HandlerTicketCloserScheduled(
        schedule=bound, environ={"TEST_HOME": "/home"}
    ).handle(tick(T0))
    assert result is not None
    assert result.unread_reason.startswith("bound profile refused")
    assert (
        result.unread_reason != "reading_source is unread: no candidate source is bound"
    )
