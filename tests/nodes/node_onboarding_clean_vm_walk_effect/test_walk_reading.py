# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The weekly clean-VM walk reading: WORKING, NOT WORKING or UNKNOWN from the walk receipt.

A walk that does not end in a delegated result reads NOT WORKING; a walk older than the
overlay's max age reads NOT WORKING; no walk at all reads UNKNOWN, which blocks like NOT
WORKING. The max age is an overlay setting with no packaged default.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.handlers.handler_onboarding_clean_vm_walk import (
    HandlerOnboardingCleanVmWalk,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.handlers.handler_walk_reading import (
    WALK_READING_OVERLAY_ID,
    HandlerWalkReading,
    load_walk_reading_overlay,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.models.model_clean_vm_walk import (
    ModelWalkReceipt,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.models.model_walk_reading import (
    ModelWalkReading,
    ModelWalkReadingRequest,
)
from tests.node_overlay_support import install_node_overlay
from tests.nodes.node_onboarding_clean_vm_walk.test_clean_vm_walk import (
    FAILED_LOG,
    StubTartRunner,
    request,
)

pytestmark = pytest.mark.unit

EIGHT_DAYS = 8 * 24 * 3600
# The injected clock: the reading reads no wall clock.
NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)


def _walk(*, failed: bool = False) -> ModelWalkReceipt:
    runner = StubTartRunner()
    if failed:
        runner.log = FAILED_LOG
    return HandlerOnboardingCleanVmWalk(runner=runner).handle(request())


def _finished_at(receipt: ModelWalkReceipt, at: datetime) -> ModelWalkReceipt:
    return receipt.model_copy(update={"finished_at": at.isoformat()})


def _read(
    receipt: ModelWalkReceipt | None, *, now: datetime = NOW, max_age: int = EIGHT_DAYS
) -> ModelWalkReading:
    return HandlerWalkReading().handle(
        ModelWalkReadingRequest(receipt=receipt, now=now, max_age_seconds=max_age)
    )


def test_reading_flips_a_delegated_walk_inside_the_window_is_working() -> None:
    reading = _read(_finished_at(_walk(), NOW - timedelta(hours=1)))

    assert reading.verdict is EnumReadingVerdict.WORKING
    assert reading.reasons == ()
    assert reading.ends_in_delegated_result
    assert not reading.verdict.blocks


def test_reading_flips_a_walk_that_does_not_end_in_a_delegated_result_is_not_working() -> (
    None
):
    reading = _read(_finished_at(_walk(failed=True), NOW - timedelta(hours=1)))

    assert reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert reading.reasons == ("walk-not-delegated",)
    assert not reading.ends_in_delegated_result
    assert reading.verdict.blocks


def test_reading_flips_the_same_walk_reads_working_then_not_working_by_injected_break() -> (
    None
):
    """The positive control: the one injected break (a failed phase) flips the reading."""
    finished = NOW - timedelta(hours=1)
    good = _read(_finished_at(_walk(), finished))
    broken = _read(_finished_at(_walk(failed=True), finished))

    assert (good.verdict, broken.verdict) == (
        EnumReadingVerdict.WORKING,
        EnumReadingVerdict.NOT_WORKING,
    )


def test_reading_flips_the_reading_is_stale_after_eight_days() -> None:
    walk = _walk()
    at_limit = _read(_finished_at(walk, NOW - timedelta(seconds=EIGHT_DAYS)))
    past_limit = _read(_finished_at(walk, NOW - timedelta(seconds=EIGHT_DAYS + 1)))

    assert at_limit.verdict is EnumReadingVerdict.WORKING
    assert past_limit.verdict is EnumReadingVerdict.NOT_WORKING
    assert past_limit.reasons == ("stale-walk",)
    assert past_limit.age_seconds == EIGHT_DAYS + 1
    # The stale walk itself did end in a delegated result: staleness is its own reason.
    assert past_limit.ends_in_delegated_result


def test_reading_flips_both_reasons_are_named_when_both_hold() -> None:
    reading = _read(
        _finished_at(_walk(failed=True), NOW - timedelta(seconds=EIGHT_DAYS + 60))
    )

    assert reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert reading.reasons == ("walk-not-delegated", "stale-walk")


def test_reading_flips_no_walk_at_all_reads_unknown_and_blocks() -> None:
    reading = _read(None)

    assert reading.verdict is EnumReadingVerdict.UNKNOWN
    assert reading.reasons == ("no-walk",)
    assert reading.verdict.blocks
    assert reading.published_at is None


def test_reading_flips_a_walk_finished_after_now_does_not_read_working() -> None:
    reading = _read(_finished_at(_walk(), NOW + timedelta(hours=1)))

    assert reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert reading.reasons == ("walk-finished-after-now",)


def test_reading_flips_an_unreadable_finish_time_is_not_a_fresh_walk() -> None:
    receipt = _walk().model_copy(update={"finished_at": "not a time"})
    reading = _read(receipt)

    assert reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert reading.reasons == ("walk-time-unreadable",)


def test_reading_flips_the_reading_carries_when_it_was_probed_and_published() -> None:
    finished = NOW - timedelta(hours=3)
    reading = _read(_finished_at(_walk(), finished))

    assert reading.probed_at == NOW
    assert reading.published_at == finished
    assert reading.max_age_seconds == EIGHT_DAYS


def test_reading_flips_a_naive_clock_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelWalkReadingRequest(
            receipt=None,
            now=datetime(2026, 11, 7, 12, 0, 0),
            max_age_seconds=EIGHT_DAYS,
        )


def test_reading_flips_max_age_comes_from_the_overlay_with_no_packaged_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    with pytest.raises(NodeOverlayNotFoundError):
        load_walk_reading_overlay()

    install_node_overlay(
        monkeypatch, tmp_path, WALK_READING_OVERLAY_ID, {"max_age_seconds": EIGHT_DAYS}
    )
    assert load_walk_reading_overlay().max_age_seconds == EIGHT_DAYS

    install_node_overlay(
        monkeypatch, tmp_path, WALK_READING_OVERLAY_ID, {"max_age_seconds": 0}
    )
    from omnimarket.handlers.node_overlay_reader import NodeOverlayError

    with pytest.raises(NodeOverlayError):
        load_walk_reading_overlay()


def _contract_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_onboarding_clean_vm_walk_effect/contract.yaml"
    )


def test_reading_flips_the_contract_routes_the_reading_operation() -> None:
    import yaml

    contract = yaml.safe_load(_contract_path().read_text())
    routes = {
        entry["operation"]: entry for entry in contract["handler_routing"]["handlers"]
    }

    assert routes["read_walk_reading"]["handler"]["name"] == "HandlerWalkReading"
    assert routes["read_walk_reading"]["output_model"].endswith(".ModelWalkReading")
    assert "walk_onboarding_in_clean_vm" in routes


def _hosted_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: StubTartRunner
) -> tuple[object, ModelWalkReceipt]:
    from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.handlers import (
        handler_onboarding_clean_vm_walk as walk,
    )
    from tests.runtime_local_compat import RuntimeLocal

    monkeypatch.setattr(walk, "select_runner", lambda _: runner)
    input_path = tmp_path / "input.json"
    input_path.write_text(request().model_dump_json())
    hosted = RuntimeLocal(
        workflow_path=_contract_path(),
        state_root=tmp_path / "state",
        input_path=input_path,
        timeout=5,
    )
    result = hosted.run()
    receipt = hosted.handler_result
    assert isinstance(receipt, ModelWalkReceipt)
    return result, receipt


def test_reading_flips_golden_chain_runtime_walk_reads_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

    result, receipt = _hosted_walk(tmp_path, monkeypatch, StubTartRunner())
    reading = _read(_finished_at(receipt, NOW - timedelta(minutes=5)))

    assert result == EnumWorkflowResult.COMPLETED
    assert reading.verdict is EnumReadingVerdict.WORKING


def test_reading_flips_error_chain_runtime_failed_walk_reads_not_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

    runner = StubTartRunner()
    runner.log = FAILED_LOG
    result, receipt = _hosted_walk(tmp_path, monkeypatch, runner)
    reading = _read(_finished_at(receipt, NOW - timedelta(minutes=5)))

    assert result == EnumWorkflowResult.FAILED
    assert reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert reading.reasons == ("walk-not-delegated",)
