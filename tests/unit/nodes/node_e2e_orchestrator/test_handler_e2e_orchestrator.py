# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B decisions of node_e2e_orchestrator: build loop to PR lifecycle bridge.

The retired consumer.py polled ``gh pr checks`` in a subprocess, read env vars and wrote files
inline. The handler here only decides: it takes one event and returns the commands to send. The CI
read and the evidence writes are node_e2e_bridge_effect's.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import BaseModel

from omnimarket.models.e2e_bridge import (
    ModelE2eCiReadCompleted,
    ModelE2eCiReadRequest,
    ModelE2eEvidenceWriteRequest,
    ModelE2ePrCiReading,
    ModelE2ePrLifecycleStart,
)
from omnimarket.nodes.node_e2e_orchestrator.handlers import handler_e2e_orchestrator
from omnimarket.nodes.node_e2e_orchestrator.handlers.handler_e2e_orchestrator import (
    HandlerE2eOrchestrator,
)
from omnimarket.nodes.node_e2e_orchestrator.models import (
    ModelE2eBuildCompleted,
    ModelE2ePrLifecycleCompleted,
)

pytestmark = pytest.mark.unit

CID = UUID("11111111-2222-3333-4444-555555555555")
BUILT_AT = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)
PR = "OmniNode-ai/omnimarket#42"


def _build(**over: object) -> ModelE2eBuildCompleted:
    base: dict[str, object] = {
        "correlation_id": CID,
        "pr_refs": (PR,),
        "cost_event_keys": ("k1", "k2"),
        "cycles_completed": 1,
        "completed_at": BUILT_AT,
    }
    base.update(over)
    return ModelE2eBuildCompleted(**base)


def _reading(overall: str, **over: object) -> ModelE2ePrCiReading:
    base: dict[str, object] = {"pr_ref": PR, "overall": overall}
    base.update(over)
    return ModelE2ePrCiReading(**base)


def _read_done(
    *readings: ModelE2ePrCiReading,
    read_at: datetime,
    attempt: int = 0,
    deadline_at: datetime | None = None,
    dry_run: bool = False,
) -> ModelE2eCiReadCompleted:
    return ModelE2eCiReadCompleted(
        correlation_id=CID,
        pr_refs=tuple(r.pr_ref for r in readings),
        attempt=attempt,
        deadline_at=deadline_at or BUILT_AT + timedelta(seconds=1500),
        timeout_seconds=1500,
        interval_seconds=30,
        dry_run=dry_run,
        read_at=read_at,
        readings=tuple(readings),
    )


def _of[M: BaseModel](out: list[BaseModel], kind: type[M]) -> list[M]:
    return [m for m in out if isinstance(m, kind)]


def _evidence(out: list[BaseModel]) -> dict[str, dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    for req in _of(out, ModelE2eEvidenceWriteRequest):
        for f in req.files:
            files[f.filename] = f.data
    return files


class TestBuildCompleted:
    def test_requests_a_ci_read_and_writes_the_manifest(self) -> None:
        out = HandlerE2eOrchestrator().handle(_build())

        (read,) = _of(out, ModelE2eCiReadRequest)
        assert read.correlation_id == CID
        assert read.pr_refs == (PR,)
        assert read.attempt == 0
        assert read.not_before is None
        assert read.deadline_at == BUILT_AT + timedelta(seconds=1500)
        assert not _of(out, ModelE2ePrLifecycleStart)
        files = _evidence(out)
        assert set(files) == {
            "build_completed.json",
            "pr_refs.json",
            "cost_event_refs.json",
            "run_manifest.json",
        }
        assert files["pr_refs.json"] == {"pr_refs": [PR]}
        assert files["cost_event_refs.json"] == {"cost_event_keys": ["k1", "k2"]}
        assert files["run_manifest.json"]["correlation_id"] == str(CID)
        assert files["run_manifest.json"]["phases"] == [
            "build_completed",
            "ci_poll",
            "merge_sweep",
        ]
        assert files["build_completed.json"]["cycles_completed"] == 1

    def test_no_pr_refs_skips_the_ci_read_and_starts_the_lifecycle(self) -> None:
        out = HandlerE2eOrchestrator().handle(_build(pr_refs=()))

        assert not _of(out, ModelE2eCiReadRequest)
        (start,) = _of(out, ModelE2ePrLifecycleStart)
        assert start.correlation_id == CID
        assert _evidence(out)["ci_checks.json"] == {"checks": [], "all_green": True}

    def test_poll_settings_ride_the_event_not_the_environment(self) -> None:
        out = HandlerE2eOrchestrator().handle(
            _build(ci_poll_timeout_seconds=60, ci_poll_interval_seconds=5)
        )

        (read,) = _of(out, ModelE2eCiReadRequest)
        assert read.timeout_seconds == 60
        assert read.interval_seconds == 5
        assert read.deadline_at == BUILT_AT + timedelta(seconds=60)


class TestCiNotGreenSelfLoop:
    def test_pending_inside_the_deadline_asks_for_another_read(self) -> None:
        read_at = BUILT_AT + timedelta(seconds=100)
        out = HandlerE2eOrchestrator().handle(
            _read_done(_reading("pending", pending=2), read_at=read_at, attempt=3)
        )

        (again,) = _of(out, ModelE2eCiReadRequest)
        assert again.attempt == 4
        assert again.not_before == read_at + timedelta(seconds=30)
        assert again.deadline_at == BUILT_AT + timedelta(seconds=1500)
        assert again.pr_refs == (PR,)
        assert not _of(out, ModelE2ePrLifecycleStart)
        assert not _of(out, ModelE2eEvidenceWriteRequest)

    def test_unreadable_is_not_green_and_keeps_polling(self) -> None:
        out = HandlerE2eOrchestrator().handle(
            _read_done(
                _reading("unreadable", error="boom"),
                read_at=BUILT_AT + timedelta(seconds=10),
            )
        )

        assert len(_of(out, ModelE2eCiReadRequest)) == 1
        assert not _of(out, ModelE2ePrLifecycleStart)

    def test_pending_past_the_deadline_gives_up_without_starting(self) -> None:
        out = HandlerE2eOrchestrator().handle(
            _read_done(_reading("pending"), read_at=BUILT_AT + timedelta(seconds=1501))
        )

        assert not _of(out, ModelE2eCiReadRequest)
        assert not _of(out, ModelE2ePrLifecycleStart)
        ci = _evidence(out)["ci_checks.json"]
        assert ci["all_green"] is False
        assert ci["status"] == "timeout"

    def test_a_red_check_stops_the_poll_at_once(self) -> None:
        out = HandlerE2eOrchestrator().handle(
            _read_done(
                _reading("red", failed=1, failing_contexts=("lint",)),
                read_at=BUILT_AT + timedelta(seconds=10),
            )
        )

        assert not _of(out, ModelE2eCiReadRequest)
        assert not _of(out, ModelE2ePrLifecycleStart)
        ci = _evidence(out)["ci_checks.json"]
        assert ci["all_green"] is False
        assert ci["status"] == "failed"
        assert ci["checks"][0]["failing_contexts"] == ["lint"]

    def test_an_unparseable_ref_will_never_turn_green_so_it_stops(self) -> None:
        out = HandlerE2eOrchestrator().handle(
            _read_done(
                _reading("unparseable", error="invalid pr_ref format"),
                read_at=BUILT_AT + timedelta(seconds=10),
            )
        )

        assert not _of(out, ModelE2eCiReadRequest)
        assert _evidence(out)["ci_checks.json"]["status"] == "failed"

    def test_one_pending_among_greens_is_still_not_green(self) -> None:
        other = "OmniNode-ai/omnibase_core#7"
        out = HandlerE2eOrchestrator().handle(
            _read_done(
                _reading("green"),
                _reading("pending", pr_ref=other),
                read_at=BUILT_AT + timedelta(seconds=10),
            )
        )

        (again,) = _of(out, ModelE2eCiReadRequest)
        assert again.pr_refs == (PR, other)
        assert not _of(out, ModelE2ePrLifecycleStart)

    def test_first_read_without_a_deadline_starts_the_clock_from_the_read(self) -> None:
        done = _read_done(_reading("pending"), read_at=BUILT_AT).model_copy(
            update={"deadline_at": None}
        )
        out = HandlerE2eOrchestrator().handle(done)

        (again,) = _of(out, ModelE2eCiReadRequest)
        assert again.deadline_at == BUILT_AT + timedelta(seconds=1500)


class TestCiGreenStartsTheLifecycle:
    def test_all_green_sends_the_start_command_and_records_the_checks(self) -> None:
        out = HandlerE2eOrchestrator().handle(
            _read_done(
                _reading("green", total=3, passed=3),
                read_at=BUILT_AT + timedelta(seconds=40),
            )
        )

        (start,) = _of(out, ModelE2ePrLifecycleStart)
        assert start.correlation_id == CID
        assert start.run_id == f"e2e-{CID}"
        assert start.dry_run is False
        assert start.enable_auto_rebase is True
        assert start.use_dag_ordering is True
        assert not _of(out, ModelE2eCiReadRequest)
        ci = _evidence(out)["ci_checks.json"]
        assert ci["all_green"] is True
        assert ci["checks"][0]["passed"] == 3

    def test_dry_run_of_the_event_reaches_the_start_command(self) -> None:
        out = HandlerE2eOrchestrator().handle(
            _read_done(
                _reading("green"),
                read_at=BUILT_AT + timedelta(seconds=40),
                dry_run=True,
            )
        )

        (start,) = _of(out, ModelE2ePrLifecycleStart)
        assert start.dry_run is True

    def test_dry_run_is_carried_from_the_build_event_into_the_read(self) -> None:
        out = HandlerE2eOrchestrator().handle(_build(dry_run=True))

        (read,) = _of(out, ModelE2eCiReadRequest)
        assert read.dry_run is True


class TestLifecycleCompletedWritesTheBundle:
    def test_merge_completed_and_receipts_are_written(self) -> None:
        out = HandlerE2eOrchestrator().handle(
            ModelE2ePrLifecycleCompleted(
                correlation_id=CID, prs_merged=3, prs_fixed=1, final_state="COMPLETE"
            )
        )

        files = _evidence(out)
        assert set(files) == {"merge_completed.json", "receipts.json"}
        assert files["merge_completed.json"]["prs_merged"] == 3
        receipts = files["receipts.json"]
        assert receipts["correlation_id"] == str(CID)
        assert receipts["merge_completed"] is True
        assert receipts["prs_merged"] == 3
        assert receipts["prs_fixed"] == 1
        assert receipts["final_state"] == "COMPLETE"
        assert len(_of(out, ModelE2eEvidenceWriteRequest)) == 1
        assert not _of(out, ModelE2ePrLifecycleStart)

    def test_the_receipt_does_not_claim_what_the_handler_cannot_see(self) -> None:
        out = HandlerE2eOrchestrator().handle(
            ModelE2ePrLifecycleCompleted(correlation_id=CID)
        )

        receipts = _evidence(out)["receipts.json"]
        assert "ci_green" not in receipts
        assert "build_completed" not in receipts


class TestHandlerIsDefinitionB:
    def test_signature_is_one_typed_request_in_a_list_of_models_out(self) -> None:
        sig = inspect.signature(HandlerE2eOrchestrator.handle)

        assert list(sig.parameters) == ["self", "request"]

    def test_no_subprocess_environment_clock_or_file_access(self) -> None:
        tree = ast.parse(inspect.getsource(handler_e2e_orchestrator))
        imported: set[str] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imported |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                imported.add(n.module)
        assert not {"subprocess", "os", "socket", "asyncio", "pathlib"} & imported
        calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        assert not {"datetime.now", "uuid4", "open", "Path"} & calls

    def test_an_unknown_request_type_is_refused(self) -> None:
        with pytest.raises(TypeError):
            HandlerE2eOrchestrator().handle(cast(Any, object()))


class TestRuntimeEntrypoint:
    """The runtime drops a bare list from ``handle``; ``handle_async`` returns publishable output."""

    async def test_handle_async_returns_the_commands_as_handler_output_events(
        self,
    ) -> None:
        from omnibase_core.models.dispatch.model_handler_output import (
            ModelHandlerOutput,
        )

        handler = HandlerE2eOrchestrator()
        built = _build()
        output = await handler.handle_async(built)

        assert isinstance(output, ModelHandlerOutput)
        assert output.correlation_id == CID
        assert list(output.events) == handler.handle(built)
        assert {type(e) for e in output.events} == {
            ModelE2eCiReadRequest,
            ModelE2eEvidenceWriteRequest,
        }

    async def test_the_envelope_id_is_deterministic(self) -> None:
        handler = HandlerE2eOrchestrator()
        first = await handler.handle_async(_build())
        second = await handler.handle_async(_build())

        assert first.input_envelope_id == second.input_envelope_id
