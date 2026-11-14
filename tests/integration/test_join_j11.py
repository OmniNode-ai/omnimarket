# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J11 (piece 11 to piece 12): a runtime-affecting merge's PASS receipt is consumed by dispatch and a lane starts from
a dispatch record (OMN-22961).

Rebuild plan 4.1, J11. Piece 11 (dev deploy and runtime) leaves a PASS lab pass receipt keyed by the merged sha; piece 12
(dispatch) consumes it and starts a lane from the immutable dispatch record. The rig runs the join handler over the
real dispatch effect, with the checker and runner commands replaced by a recorder, and reads the result back through
the dispatch reading. Nothing here touches a real host, the ledger or GitHub.

Golden: the PASS receipt is consumed, the lane is placed from a content-addressed record on disk, and the reading
over that lane and its record is WORKING.
Error: a lane with no dispatch record is named by the reading, and a missing, failed, unreadable, foreign or
pre-merge receipt starts nothing: the dispatch effect never runs a command and writes no record.

Run the two cases with ``-k golden`` and ``-k error``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.lane_dispatch.model_lane_dispatch import ModelLaneDispatchRecord
from omnimarket.models.model_dev_deploy_reading import ModelLabPassReceiptFact
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_dispatch_reading_compute.handlers.handler_dispatch_reading import (
    SUBJECT_LANES_WITHOUT_RECORD,
    HandlerDispatchReading,
)
from omnimarket.nodes.node_dispatch_reading_compute.models import (
    ModelDispatchReadingRequest,
    ModelDispatchReadingSettings,
    ModelDispatchRecord,
    ModelLaneFact,
)
from omnimarket.nodes.node_lane_dispatch_effect.handlers.handler_dispatch_join_j11 import (
    HandlerDispatchJoinJ11,
    load_join_j11_policy,
)
from omnimarket.nodes.node_lane_dispatch_effect.handlers.handler_lane_dispatch import (
    HandlerLaneDispatch,
)
from omnimarket.nodes.node_lane_dispatch_effect.models.model_join_j11 import (
    EnumJoinJ11Outcome,
    ModelJoinJ11Request,
    ModelJoinJ11Result,
)
from tests.nodes.test_lane_dispatch_contract import effect_overlay, spec_data

pytestmark = pytest.mark.integration

POLICY = load_join_j11_policy()
LANE = "dispatch-proof"
REQUIRED_LANE = "fixture-lane"
MERGED = "a" * 40
OTHER = "b" * 40
MERGED_AT = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
JUDGED_AT = datetime(2026, 11, 4, 16, 1, tzinfo=UTC)


class _Commands:
    """The checker and the runner, replaced by a recorder that answers like both accepting."""

    def __init__(self, *answers: tuple[int, str]) -> None:
        self.answers = iter(answers)
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str], timeout_s: int) -> tuple[int, str]:
        self.calls.append(tuple(argv))
        return next(self.answers)


def _accepted() -> _Commands:
    return _Commands(
        (0, "brief accepted"),
        (0, f"PLACEMENT lane={LANE} status=running host=h053 receipt=/run/job.json"),
    )


class _Receipts:
    def __init__(
        self, *receipts: ModelLabPassReceiptFact, unread: bool = False
    ) -> None:
        self._receipts = receipts
        self._unread = unread
        self.read: list[str] = []

    def read_receipts(self, sha: str) -> tuple[ModelLabPassReceiptFact, ...] | None:
        self.read.append(sha)
        if self._unread:
            return None
        return tuple(r for r in self._receipts if r.sha == sha)


def _receipt(
    *,
    sha: str = MERGED,
    result: str = "PASS",
    lane: str = REQUIRED_LANE,
    minutes: float = 20,
) -> ModelLabPassReceiptFact:
    return ModelLabPassReceiptFact.model_validate(
        {
            "sha": sha,
            "lane": lane,
            "result": result,
            "finished_at": MERGED_AT + timedelta(minutes=minutes),
        }
    )


def _request(tmp_path: Path, *, runtime_affecting: bool = True) -> ModelJoinJ11Request:
    return ModelJoinJ11Request(
        correlation_id="j11-corr",
        merged_sha=MERGED,
        merged_at=MERGED_AT,
        judged_at=JUDGED_AT,
        runtime_affecting=runtime_affecting,
        required_lane=REQUIRED_LANE,
        record_json=json.dumps(spec_data()),
        output_dir=str(tmp_path),
    )


def _join(receipts: _Receipts, commands: _Commands) -> HandlerDispatchJoinJ11:
    return HandlerDispatchJoinJ11(
        receipts=receipts,
        dispatch=HandlerLaneDispatch(effect_overlay(), commands, lambda: JUDGED_AT),
        policy=POLICY,
    )


def _settings() -> ModelDispatchReadingSettings:
    overlay: dict[str, Any] = yaml.safe_load(
        files("omnimarket.nodes.node_dispatch_reading_compute")
        .joinpath("overlay.yaml")
        .read_text()
    )
    return ModelDispatchReadingSettings.model_validate(overlay)


def _read(
    lanes: tuple[ModelLaneFact, ...], records: tuple[ModelDispatchRecord, ...]
) -> Any:
    return HandlerDispatchReading().handle(
        ModelDispatchReadingRequest(
            correlation_id="j11-corr",
            now=JUDGED_AT + timedelta(minutes=5),
            read_at=JUDGED_AT + timedelta(minutes=4),
            settings=_settings(),
            lanes=lanes,
            records=records,
        )
    )


def _lane(
    name: str, *, launched_from: str | None = "onex-remote-lane"
) -> ModelLaneFact:
    return ModelLaneFact(
        lane=name,
        kind="fill",
        started_at=JUDGED_AT + timedelta(seconds=1),
        launched_from=launched_from,
    )


def _record_from_disk(result: ModelJoinJ11Result) -> ModelDispatchRecord:
    """The dispatch record the started lane left on disk, in the reading's own terms."""
    assert result.dispatch is not None
    on_disk = ModelLaneDispatchRecord.model_validate_json(
        Path(result.dispatch.record_path).read_text()
    )
    spec = on_disk.spec
    return ModelDispatchRecord(
        dispatch_id=spec.job_id,
        lane=spec.lane,
        dispatched_at=JUDGED_AT,
        deadline_at=JUDGED_AT + timedelta(minutes=spec.time_box_min),
    )


def test_policy_reads_the_pieces_and_codes_from_the_contract() -> None:
    assert POLICY.join_id == "J11"
    assert (POLICY.from_piece, POLICY.to_piece) == (11, 12)
    assert (
        len(
            {
                POLICY.receipt_missing_code,
                POLICY.receipt_failed_code,
                POLICY.receipts_unreadable_code,
                POLICY.dispatch_not_placed_code,
            }
        )
        == 4
    ), "every cause has its own typed code"


# ---------------------------------------------------------------------------------------------- golden


def test_golden_pass_receipt_is_consumed_and_a_lane_starts_from_the_dispatch_record(
    tmp_path: Path,
) -> None:
    commands = _accepted()
    receipts = _Receipts(_receipt(), _receipt(sha=OTHER, result="FAIL"))
    result = _join(receipts, commands).handle(_request(tmp_path))
    assert result.outcome is EnumJoinJ11Outcome.STARTED
    assert result.reason_code is None
    assert receipts.read == [MERGED], (
        "the receipt of the exact merged sha is the one read"
    )
    assert result.dispatch is not None
    assert result.dispatch.status == "placed"
    assert result.dispatch.lane == LANE
    assert len(commands.calls) == 2, "the brief checker, then the runner"
    runner = commands.calls[1]
    assert "--detach" in runner
    assert runner[runner.index("--lane") + 1] == LANE
    # The lane starts from the content-addressed record, which is on disk and verifies.
    record_path = Path(result.dispatch.record_path)
    assert record_path.name == f"{result.dispatch.content_hash}.job.json"
    on_disk = ModelLaneDispatchRecord.model_validate_json(record_path.read_text())
    assert on_disk.content_hash == result.dispatch.content_hash
    assert on_disk.spec.lane == LANE


def test_golden_the_started_lane_has_a_dispatch_record_and_the_reading_is_working(
    tmp_path: Path,
) -> None:
    result = _join(_Receipts(_receipt()), _accepted()).handle(_request(tmp_path))
    assert result.outcome is EnumJoinJ11Outcome.STARTED
    reading = _read((_lane(LANE),), (_record_from_disk(result),))
    assert reading.verdict is EnumReadingVerdict.WORKING
    assert reading.lanes_without_record == ()
    assert reading.unreconciled == ()


def test_golden_a_merge_that_is_not_runtime_affecting_needs_no_receipt_and_starts_nothing(
    tmp_path: Path,
) -> None:
    commands = _Commands()
    receipts = _Receipts()
    result = _join(receipts, commands).handle(
        _request(tmp_path, runtime_affecting=False)
    )
    assert result.outcome is EnumJoinJ11Outcome.EXEMPT
    assert receipts.read == []
    assert commands.calls == []


# ---------------------------------------------------------------------------------------------- error


def test_error_a_lane_with_no_dispatch_record_is_named_by_the_reading() -> None:
    scratch = "bash .claude_scratch/drill-run-244.sh"
    reading = _read(
        (_lane(LANE), _lane("stray-lane", launched_from=scratch)),
        (
            ModelDispatchRecord(
                dispatch_id="dispatch-1",
                lane=LANE,
                dispatched_at=JUDGED_AT,
                deadline_at=JUDGED_AT + timedelta(hours=1),
            ),
        ),
    )
    assert reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert [(n.lane, n.from_scratch_script) for n in reading.lanes_without_record] == [
        ("stray-lane", True)
    ]
    subject = next(
        s for s in reading.subjects if s.name == SUBJECT_LANES_WITHOUT_RECORD
    )
    assert subject.verdict is EnumReadingVerdict.NOT_WORKING
    assert "stray-lane" in subject.reason
    assert scratch in subject.reason


@pytest.mark.parametrize(
    ("receipts", "code"),
    [
        pytest.param(_Receipts(), POLICY.receipt_missing_code, id="no-receipt-at-all"),
        pytest.param(
            _Receipts(_receipt(result="FAIL")),
            POLICY.receipt_failed_code,
            id="fail-receipt",
        ),
        pytest.param(
            _Receipts(unread=True),
            POLICY.receipts_unreadable_code,
            id="store-unreadable",
        ),
        pytest.param(
            _Receipts(_receipt(sha=OTHER)),
            POLICY.receipt_missing_code,
            id="receipt-of-another-sha",
        ),
        pytest.param(
            _Receipts(_receipt(lane="some-other-lane")),
            POLICY.receipt_missing_code,
            id="receipt-of-another-lane",
        ),
        pytest.param(
            _Receipts(_receipt(minutes=-5)),
            POLICY.receipt_missing_code,
            id="receipt-from-before-the-merge",
        ),
    ],
)
def test_error_a_missing_receipt_starts_nothing(
    tmp_path: Path, receipts: _Receipts, code: str
) -> None:
    commands = _Commands()
    result = _join(receipts, commands).handle(_request(tmp_path))
    assert result.outcome is EnumJoinJ11Outcome.NOT_STARTED
    assert result.reason_code == code
    assert result.dispatch is None
    assert commands.calls == [], "neither the checker nor the runner ran"
    assert list(tmp_path.iterdir()) == [], "no dispatch record or brief was written"
    # With nothing started, the reading has no lane to judge and never reads WORKING.
    assert _read((), ()).verdict is EnumReadingVerdict.UNKNOWN


def test_error_a_pass_receipt_beside_a_refused_dispatch_is_not_a_started_lane(
    tmp_path: Path,
) -> None:
    commands = _Commands((3, "brief refused: merged ticket"))
    result = _join(_Receipts(_receipt()), commands).handle(_request(tmp_path))
    assert result.outcome is EnumJoinJ11Outcome.NOT_STARTED
    assert result.reason_code == POLICY.dispatch_not_placed_code
    assert result.dispatch is not None
    assert result.dispatch.status == "refused"
    assert "merged ticket" in result.detail
    assert len(commands.calls) == 1, "the runner never ran after the checker refused"
