# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22917, plan 4.3: the seven readings that prove no file ledger run as one node, each red on its planted break.

The control is a world with no file ledger: all seven read WORKING. Each break below changes one fact of
that world, and exactly that reading turns NOT_WORKING while the other six stay WORKING.
"""

from __future__ import annotations

import pytest

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_work_ledger_no_file_readings_effect.models import (
    ModelAbsenceFacts,
    ModelAppendReceipt,
    ModelCensusFacts,
    ModelFileProbeFacts,
    ModelNoFileFacts,
    ModelNoFileRequest,
    ModelPublishFacts,
    ModelRebuildFacts,
    ModelReceiptFacts,
    ModelRestoreFacts,
)
from tests.unit.nodes.node_work_ledger_reader_effect.no_file_world import (
    ROWS,
    clean_world,
    overlay,
    readings,
    world_source,
)

pytestmark = pytest.mark.unit


def planted(**change: object) -> ModelNoFileFacts:
    return clean_world().model_copy(update=change)


BREAKS = [
    (
        1,
        "file_probe",
        ModelFileProbeFacts(
            paths_present=("ledger-path",),
            receipt=ModelAppendReceipt(ledger_seq=1, file_line=False, read_back=True),
        ),
        "file-present-ledger-path",
    ),
    (
        2,
        "census",
        ModelCensusFacts(total_files=1, own_line_files=0, units_naming_path=()),
        "census-files-1",
    ),
    (
        3,
        "absence",
        ModelAbsenceFacts(
            hours_absent=1.5,
            writer_failures=0,
            readers_restarted=12,
            file_fallback_lines=0,
            query_read_exit=1,
        ),
        "query-read-exit-1",
    ),
    (
        4,
        "rebuild",
        ModelRebuildFacts(live=ROWS, rebuilt=ROWS[:-1]),
        "rebuilt-table-lacks-seq-5-of-1",
    ),
    (
        5,
        "restore",
        ModelRestoreFacts(
            ran=True, snapshot_rows=5, restored_rows=5, checksum_ok=False
        ),
        "snapshot-checksum-failed",
    ),
    (
        6,
        "publish",
        ModelPublishFacts(hours_since_rows_through_commit=60.0, job_listed=True),
        "publish-job-still-listed",
    ),
    (
        7,
        "receipt",
        ModelReceiptFacts(
            compared=("table", "accepted_row_topic", "projection"),
            exact=False,
            mismatches=1,
        ),
        "receipt-compares-extra-source-projection",
    ),
]


def verdicts(world: ModelNoFileFacts) -> dict[int, EnumReadingVerdict]:
    node, _ = readings(world_source(world))
    result = node.handle(ModelNoFileRequest(correlation_id="r"))
    assert [r.number for r in result.readings] == [1, 2, 3, 4, 5, 6, 7]
    return {r.number: r.verdict for r in result.readings}


def test_control_world_reads_all_seven_working() -> None:
    node, _ = readings(world_source(clean_world()))
    result = node.handle(ModelNoFileRequest(correlation_id="control"))
    assert result.all_working
    assert {r.verdict for r in result.readings} == {EnumReadingVerdict.WORKING}
    assert all(r.injected_fault for r in result.readings)


@pytest.mark.parametrize(("number", "field", "broken", "reason"), BREAKS)
def test_each_planted_break_turns_its_own_reading_red(
    number: int, field: str, broken: object, reason: str
) -> None:
    got = verdicts(planted(**{field: broken}))
    assert got[number] is EnumReadingVerdict.NOT_WORKING
    assert {n: v for n, v in got.items() if n != number} == {
        n: EnumReadingVerdict.WORKING for n in range(1, 8) if n != number
    }
    node, _ = readings(world_source(planted(**{field: broken})))
    detail = node.handle(ModelNoFileRequest(correlation_id="why")).readings[number - 1]
    assert reason in detail.reasons


def test_the_seven_names_the_overlay_declares_are_the_seven_of_the_plan() -> None:
    assert [r.number for r in overlay().readings] == [1, 2, 3, 4, 5, 6, 7]
    assert len({r.injected_fault for r in overlay().readings}) == 7


def test_more_than_one_break_turns_each_of_their_readings_red() -> None:
    both = planted(file_probe=BREAKS[0][2], publish=BREAKS[5][2])
    got = verdicts(both)
    red = {n for n, v in got.items() if v is EnumReadingVerdict.NOT_WORKING}
    assert red == {1, 6}


@pytest.mark.parametrize(
    ("change", "number"),
    [
        (
            {
                "receipt": ModelAppendReceipt(
                    ledger_seq=None, file_line=False, read_back=True
                )
            },
            1,
        ),
        (
            {
                "receipt": ModelAppendReceipt(
                    ledger_seq=5, file_line=True, read_back=True
                )
            },
            1,
        ),
        (
            {
                "receipt": ModelAppendReceipt(
                    ledger_seq=5, file_line=False, read_back=False
                )
            },
            1,
        ),
    ],
)
def test_reading_one_also_fails_on_a_receipt_without_a_seq_or_with_a_file_line(
    change: dict[str, object], number: int
) -> None:
    probe = ModelFileProbeFacts(paths_present=(), receipt=change["receipt"])  # type: ignore[arg-type]
    assert verdicts(planted(file_probe=probe))[number] is EnumReadingVerdict.NOT_WORKING


def test_reading_three_requires_the_hour_and_no_fallback() -> None:
    short = ModelAbsenceFacts(
        hours_absent=0.2,
        writer_failures=0,
        readers_restarted=3,
        file_fallback_lines=0,
        query_read_exit=0,
    )
    fallback = short.model_copy(update={"hours_absent": 2.0, "file_fallback_lines": 4})
    assert verdicts(planted(absence=short))[3] is EnumReadingVerdict.NOT_WORKING
    assert verdicts(planted(absence=fallback))[3] is EnumReadingVerdict.NOT_WORKING


def test_reading_four_compares_row_for_row_and_sequence_for_sequence() -> None:
    changed = tuple(
        r if r.seq != 3 else r.model_copy(update={"digest": "x"}) for r in ROWS
    )
    assert (
        verdicts(planted(rebuild=ModelRebuildFacts(live=ROWS, rebuilt=changed)))[4]
        is EnumReadingVerdict.NOT_WORKING
    )
    assert (
        verdicts(planted(rebuild=ModelRebuildFacts(live=(), rebuilt=())))[4]
        is EnumReadingVerdict.UNKNOWN
    )


def test_reading_six_is_red_on_a_recent_rows_through_commit() -> None:
    recent = ModelPublishFacts(hours_since_rows_through_commit=3.0, job_listed=False)
    assert verdicts(planted(publish=recent))[6] is EnumReadingVerdict.NOT_WORKING
    never = ModelPublishFacts(hours_since_rows_through_commit=None, job_listed=False)
    assert verdicts(planted(publish=never))[6] is EnumReadingVerdict.WORKING
