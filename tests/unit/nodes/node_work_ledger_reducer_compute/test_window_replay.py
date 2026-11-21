# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The window replay re-judges a trailing window of accepted rows and the refused requests (OMN-25629, L11).

Golden chain: seventeen days of accepted rows, the last fourteen replayed, come back with an empty difference
list, and the refused requests of the window are refused under the rule name the file judge gave, judged on the
rows accepted before their time. Error chain: a changed rule, an unreadable refusal and a refusal the guards no
longer give are each listed. Failing control: a deliberately altered guard makes the same run's list non-empty,
and the command exits non-zero.
"""

from __future__ import annotations

import inspect
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from click.testing import CliRunner
from omnibase_core.validation.validator_node_purity import ValidatorNodePurity

from omnimarket.handlers.ledger_row_grammar import GOVERNED_LEDGER_NAMES, parse_row
from omnimarket.models.work_ledger_append.model_work_ledger_append import (
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.__main__ import main
from omnimarket.nodes.node_work_ledger_reducer_compute.handlers.handler_work_ledger_reducer import (
    HandlerWorkLedgerReducer,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.logic.work_ledger_judge import (
    judge_row,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.logic.work_ledger_window_replay import (
    WorkLedgerWindowReplay,
    _StateIndex,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.logic.work_ledger_window_sources import (
    join_refusals,
    recorded_requests,
    serve_log_refusals,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_judge import (
    EnumJudgeVerdict,
    ModelJudgeVerdict,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_reducer import (
    ModelWorkLedgerReducerRequest,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_window_replay import (
    ModelRefusedRequest,
    ModelWindowReplayRequest,
)

pytestmark = pytest.mark.unit

LEDGER = next(iter(GOVERNED_LEDGER_NAMES))
START = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
DAYS = 17
FIRST_REQUEST = UUID("11111111-1111-4111-8111-111111111111")
SECOND_REQUEST = UUID("22222222-2222-4222-8222-222222222222")
REUSE_DAY = 6
COLLISION_DAY = 9


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _hold(at: datetime, lane: str, surface: str, until_hours: int = 6) -> str:
    return (
        f"{_stamp(at)} | HOLD | lane={lane} | id={_stamp(at)}-{lane} | surface={surface} | "
        f"until={_stamp(at + timedelta(hours=until_hours))} | reserved"
    )


def _accepted_rows() -> list[str]:
    """Seventeen days, three rows a day: a hold, its release two hours on, a status."""
    rows: list[str] = []
    for day in range(DAYS):
        at = START + timedelta(days=day)
        rows.append(_hold(at, "beta", "lab-dev"))
        release_at = at + timedelta(hours=2)
        rows.append(
            f"{_stamp(release_at)} | RELEASE | lane=beta | re={_stamp(at)}-beta | surface=lab-dev | "
            "result=PASS | restored=yes | done"
        )
        status_at = at + timedelta(hours=3)
        rows.append(
            f"{_stamp(status_at)} | STATUS | lane=alpha | ticket=OMN-1 | progress"
        )
    return rows


def _ledger_before(rows: list[str], when: datetime) -> str:
    return "\n".join(
        r
        for r in rows
        if (row := parse_row(r)) is not None and row.stamp < _stamp(when)
    )


def _refused(rows: list[str]) -> list[ModelRefusedRequest]:
    """Two requests the live judge refused: an id reuse, and a surface taken while another lane's hold is live."""
    reuse_at = START + timedelta(days=REUSE_DAY, hours=4)
    reuse = _hold(
        START + timedelta(days=REUSE_DAY), "beta", "lab-x"
    )  # same id as the day's hold
    reuse_refusal = judge_row(reuse, _ledger_before(rows, reuse_at), LEDGER)
    assert reuse_refusal.verdict is EnumJudgeVerdict.REFUSE
    collide_at = START + timedelta(days=COLLISION_DAY, hours=1)
    collide = _hold(collide_at, "gamma", "lab-dev", until_hours=2)
    collide_refusal = judge_row(collide, _ledger_before(rows, collide_at), LEDGER)
    assert collide_refusal.verdict is EnumJudgeVerdict.REFUSE
    assert collide_refusal.reason is not None
    assert reuse_refusal.reason is not None
    return [
        ModelRefusedRequest(
            request_id=str(FIRST_REQUEST),
            rows=reuse,
            requested_at=reuse_at,
            file_judge_message=reuse_refusal.reason,
        ),
        ModelRefusedRequest(
            request_id=str(SECOND_REQUEST),
            rows=collide,
            requested_at=collide_at,
            file_judge_message=collide_refusal.reason,
        ),
    ]


def _request(**changes: object) -> ModelWindowReplayRequest:
    rows = _accepted_rows()
    values: dict[str, object] = {
        "ledger_name": LEDGER,
        "accepted_rows": tuple(rows),
        "refused_requests": tuple(_refused(rows)),
    }
    values.update(changes)
    return ModelWindowReplayRequest.model_validate(values)


def test_guard_replay_14_days_accepts_every_accepted_row_with_an_empty_list() -> None:
    result = WorkLedgerWindowReplay().handle(_request(refused_requests=()))
    end = START + timedelta(days=DAYS - 1, hours=3)
    assert result.window_end == end
    assert result.window_start == end - timedelta(days=14)
    in_window = [
        r
        for r in _accepted_rows()
        if (row := parse_row(r)) is not None
        and row.stamp >= _stamp(end - timedelta(days=14))
    ]
    assert result.rows_judged == len(in_window) == 3 * 14 + 1
    assert result.row_differences == ()
    assert result.equivalent is True


def test_guard_replay_14_days_judges_a_hold_accepted_before_the_window() -> None:
    # The window opens inside day 3's hold; the id reuse on that day's hold is judged on the row before the window.
    rows = _accepted_rows()
    reuse = _hold(START + timedelta(days=2), "beta", "lab-x")
    assert judge_row(reuse, "\n".join(rows), LEDGER).verdict is EnumJudgeVerdict.REFUSE
    index = _StateIndex()
    for line in rows:
        index.add(line)
    assert judge_row(reuse, index.relevant_text(reuse), LEDGER).verdict is (
        EnumJudgeVerdict.REFUSE
    )
    assert judge_row(reuse, "", LEDGER).verdict is EnumJudgeVerdict.ACCEPT


def test_guard_replay_14_days_bounded_state_gives_the_verdict_the_whole_ledger_gives() -> (
    None
):
    rows = _accepted_rows()
    index = _StateIndex()
    probes = [
        _hold(START + timedelta(days=4), "beta", "lab-y"),  # id reuse
        _hold(START + timedelta(days=4, hours=1), "gamma", "lab-dev"),  # lease taken
        _hold(START + timedelta(days=4, hours=3), "gamma", "lab-dev"),  # lease released
        _hold(START + timedelta(days=30), "gamma", "lab-dev"),  # a fresh hold
        f"{_stamp(START + timedelta(days=4, hours=5))} | STATUS | lane=alpha | ticket=OMN-1 | x",
    ]
    for line in rows:
        index.add(line)
    verdicts = []
    for probe in probes:
        full = judge_row(probe, "\n".join(rows), LEDGER)
        bounded = judge_row(probe, index.relevant_text(probe), LEDGER)
        assert bounded == full
        verdicts.append(full.verdict)
    assert EnumJudgeVerdict.REFUSE in verdicts
    assert EnumJudgeVerdict.ACCEPT in verdicts
    assert len(index.relevant_text(probes[0]).splitlines()) < len(rows)


def test_refused_request_replay_refuses_each_request_under_the_file_judges_rule() -> (
    None
):
    result = WorkLedgerWindowReplay().handle(_request())
    assert result.refused_judged == 2
    assert result.refusal_differences == ()
    assert result.equivalent is True
    assert result.difference_count == 0


def test_refused_request_replay_judges_a_request_on_the_ledger_at_its_time() -> None:
    # The day-9 lease collision is refused only while beta's hold is live; the hold's RELEASE comes two hours
    # after the hold. Judged on the ledger's end state the hold is released and the request is accepted.
    request = _request()
    collision = request.refused_requests[1]
    end_state = judge_row(collision.rows, "\n".join(request.accepted_rows), LEDGER)
    assert end_state.verdict is EnumJudgeVerdict.ACCEPT
    assert WorkLedgerWindowReplay().handle(request).refusal_differences == ()


def test_refused_request_replay_lists_a_changed_rule_a_lost_refusal_and_an_unreadable_one() -> (
    None
):
    request = _request()
    changed = request.refused_requests[0].model_copy(
        update={"file_judge_message": "OMN-1 an older rule -- reason"}
    )
    lost = request.refused_requests[1].model_copy(
        update={"requested_at": START + timedelta(days=COLLISION_DAY, hours=3)}
    )
    result = WorkLedgerWindowReplay().handle(
        request.model_copy(
            update={
                "refused_requests": (changed, lost),
                "unreadable_refusals": ("ffffffff-ffff-4fff-8fff-ffffffffffff",),
            }
        )
    )
    assert [d.row_ref for d in result.refusal_differences] == [
        str(FIRST_REQUEST),
        str(SECOND_REQUEST),
    ]
    assert result.refusal_differences[0].difference.startswith(
        "both refused; file judge rule OMN-1 an older rule"
    )
    assert result.refusal_differences[1].replayed.verdict is EnumJudgeVerdict.ACCEPT
    assert result.refusal_differences[1].difference.startswith("replayed accepted")
    assert result.unreadable_refusals == ("ffffffff-ffff-4fff-8fff-ffffffffffff",)
    assert result.equivalent is False
    assert result.difference_count == 3


def test_refused_request_replay_requests_before_the_window_are_not_judged() -> None:
    request = _request()
    early = request.refused_requests[0].model_copy(
        update={"requested_at": START + timedelta(days=1)}
    )
    result = WorkLedgerWindowReplay().handle(
        request.model_copy(update={"refused_requests": (early,)})
    )
    assert result.refused_judged == 0


def _altered(row: str, existing: str, ledger_name: str) -> ModelJudgeVerdict:
    if " | RELEASE | " in row:  # the planted break: the guards refuse every RELEASE
        return ModelJudgeVerdict(
            verdict=EnumJudgeVerdict.REFUSE, rule="OMN-0 altered guard", reason="x"
        )
    return judge_row(row, existing, ledger_name)


def test_guard_replay_14_days_failing_control_an_altered_guard_lists_differences() -> (
    None
):
    request = _request(refused_requests=())
    assert WorkLedgerWindowReplay().handle(request).row_differences == ()
    broken = WorkLedgerWindowReplay(judge=_altered).handle(request)
    assert broken.equivalent is False
    assert len(broken.row_differences) == 14
    assert {d.replayed.rule for d in broken.row_differences} == {"OMN-0 altered guard"}


def test_the_reducer_node_routes_window_replay_to_the_replay() -> None:
    result = HandlerWorkLedgerReducer().handle(
        ModelWorkLedgerReducerRequest(operation="window_replay", window=_request())
    )
    assert result.window is not None
    assert result.window.equivalent is True


def test_an_empty_ledger_replays_to_an_empty_window() -> None:
    result = WorkLedgerWindowReplay().handle(
        ModelWindowReplayRequest(ledger_name=LEDGER, accepted_rows=())
    )
    assert (result.rows_judged, result.equivalent, result.window_start) == (
        0,
        True,
        None,
    )


def test_the_serve_log_and_the_request_record_join_by_request_id() -> None:
    request = _request()
    refused = request.refused_requests[0]
    log = (
        "2026-11-07 09:00:00 WARNING work-ledger host h1 request "
        f"{refused.request_id} refused: {refused.file_judge_message}\n"
        "2026-11-07 09:00:01 INFO work-ledger host h1 request "
        f"{SECOND_REQUEST} status=accepted exit=0 lines=[1]\n"
        "2026-11-07 09:00:02 WARNING work-ledger host h1 request "
        "33333333-3333-4333-8333-333333333333 refused: OMN-1 gone -- r\n"
    )
    record = ModelWorkLedgerAppendRequest(
        request_id=FIRST_REQUEST,
        rows=refused.rows,
        requested_by_lane="beta",
        requesting_host="h1",
        requested_at=refused.requested_at,
    ).model_dump_json()
    joined, unreadable = join_refusals(
        serve_log_refusals(log), recorded_requests(record + "\n")
    )
    assert [item.request_id for item in joined] == [str(FIRST_REQUEST)]
    assert joined[0].requested_at == refused.requested_at
    assert unreadable == ("33333333-3333-4333-8333-333333333333",)


def _write_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    rows = _accepted_rows()
    archive = tmp_path / "archive"
    archive.mkdir()
    half = len(rows) // 2
    (archive / "ROLLING_WORK_LEDGER_roll.md").write_text(
        "# archive\n" + "\n".join(rows[:half]) + "\n", encoding="utf-8"
    )
    ledger = tmp_path / LEDGER
    ledger.write_text("# ledger\n" + "\n".join(rows[half:]) + "\n", encoding="utf-8")
    refused = _refused(rows)
    log = tmp_path / "serve.log"
    log.write_text(
        "".join(
            f"WARNING work-ledger host h1 request {r.request_id} refused: {r.file_judge_message}\n"
            for r in refused
        ),
        encoding="utf-8",
    )
    requests = tmp_path / "requests.jsonl"
    requests.write_text(
        "".join(
            ModelWorkLedgerAppendRequest(
                request_id=UUID(r.request_id),
                rows=r.rows,
                requested_by_lane="beta",
                requesting_host="h1",
                requested_at=r.requested_at,
            ).model_dump_json()
            + "\n"
            for r in refused
        ),
        encoding="utf-8",
    )
    return ledger, archive, log, requests


def _invoke(tmp_path: Path, *extra: str) -> tuple[int, str]:
    ledger, archive, log, requests = _write_inputs(tmp_path)
    result = CliRunner().invoke(
        main,
        [
            "--ledger",
            str(ledger),
            "--archive-dir",
            str(archive),
            "--serve-log",
            str(log),
            "--requests",
            str(requests),
            *extra,
        ],
    )
    return result.exit_code, result.output


def test_guard_replay_14_days_command_prints_zero_differences_and_exits_zero(
    tmp_path: Path,
) -> None:
    code, output = _invoke(tmp_path)
    assert code == 0, output
    assert output.splitlines()[0].startswith(
        "differences=0 rows_judged=43 refused_judged=2"
    )


def test_guard_replay_14_days_command_with_a_planted_guard_prints_differences_and_exits_non_zero(
    tmp_path: Path,
) -> None:
    code, output = _invoke(tmp_path, "--plant-guard-break", "RELEASE")
    assert code == 1, output
    assert output.splitlines()[0].startswith("differences=14 ")
    assert "OMN-25629 planted guard break" in output


def test_the_command_refuses_a_serve_log_without_its_request_record(
    tmp_path: Path,
) -> None:
    ledger, _archive, log, _ = _write_inputs(tmp_path)
    result = CliRunner().invoke(
        main, ["--ledger", str(ledger), "--serve-log", str(log)]
    )
    assert result.exit_code == 2
    assert "go together" in result.output


def test_command_form_is_pure_without_a_suppression_comment() -> None:
    """The command module passes the node-purity gate with every comment stripped, so no suppression carries it (OMN-26342)."""
    assert main.callback is not None
    source_file = inspect.getsourcefile(sys.modules[main.callback.__module__])
    assert source_file is not None
    path = Path(source_file)
    stripped = "\n".join(
        re.sub(r"\s+#.*$", "", line)
        for line in path.read_text(encoding="utf-8").splitlines()
    )
    assert ValidatorNodePurity().check_source(path, stripped) == []
