# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane trace (OMN-24072): one lane's completed calls with full stored output, a typed label, the exit code and the lane,
ending in an outcomes line. The labels come from the real outcome handler with the outcome node's example overlay, so a
refusal that ended with exit code 0 is read by the same rules the running node uses."""

from __future__ import annotations

import io
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_hook_lane_trace_compute.handlers.handler_hook_lane_trace import (
    HandlerHookLaneTrace,
    build_sql,
    display_output,
)
from omnimarket.nodes.node_hook_lane_trace_compute.models import (
    EnumLaneCallLabel,
    ModelLaneTraceRequest,
    ModelLaneTraceRow,
)
from omnimarket.nodes.node_hook_lane_trace_compute.runtime_hook_lane_trace import main
from omnimarket.nodes.node_hook_tool_outcome_compute.handlers.handler_hook_tool_outcome import (
    HandlerHookToolOutcome,
)
from omnimarket.nodes.node_hook_tool_outcome_compute.models.model_hook_tool_outcome import (
    ModelToolOutcomeRules,
)

EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_hook_tool_outcome_compute"
    / "overlay.example.yaml"
)
LANE = "fill-ticket-OMN-24072-20261114T0408Z"
SINCE, UNTIL = "2026-11-14T03:30:00Z", "2026-11-14T04:30:00Z"
NOW = datetime(2026, 11, 14, 4, 30, tzinfo=UTC)
REFUSAL = (
    "REFUSED 64 args: a TERMINAL row needs --actor and/or --model (who ran this lane)"
)


def rules() -> ModelToolOutcomeRules:
    return ModelToolOutcomeRules.model_validate(
        yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    )


def handler() -> HandlerHookLaneTrace:
    return HandlerHookLaneTrace(HandlerHookToolOutcome(rules()))


def bash_out(stdout: str, stderr: str = "") -> str:
    return json.dumps(
        {
            "interrupted": False,
            "isImage": False,
            "noOutputExpected": False,
            "stderr": stderr,
            "stdout": stdout,
        }
    )


def row(
    n: int,
    hook: str = "PostToolUse",
    exit_code: int | None = 0,
    kind: str = "tool_response",
    out: str | None = None,
    tool: str = "Bash",
) -> ModelLaneTraceRow:
    return ModelLaneTraceRow(
        emitted_at=f"2026-11-14T04:{n:02d}:00Z",
        session_id="sess-aaaaaaaa",
        tool_use_id=f"toolu_{n:04d}abcdefgh",
        tool_name=tool,
        hook_event_name=hook,  # type: ignore[arg-type]
        exit_code=exit_code,
        lane=LANE,
        content_kind=kind if out is not None else None,  # type: ignore[arg-type]
        output=out,
    )


def trace(*rows: ModelLaneTraceRow, limit: int = 500) -> list[str]:
    request = ModelLaneTraceRequest(
        lane=LANE, since=SINCE, until=UNTIL, limit=limit, rows=rows
    )
    return list(handler().handle(request).lines)


def test_each_call_prints_label_exit_code_lane_and_full_output_and_the_count_line_ends_the_read() -> (
    None
):
    long_out = "\n".join(f"line {i}" for i in range(40))
    lines = trace(
        row(1, out=bash_out("hello\nworld")),
        row(
            2,
            hook="PostToolUseFailure",
            exit_code=1,
            kind="tool_error",
            out="Exit code 1\nboom",
        ),
        row(3, out=bash_out(REFUSAL)),
        row(
            4,
            exit_code=124,
            kind="tool_error",
            hook="PostToolUseFailure",
            out="Command timed out after 120s",
        ),
        row(5, out=bash_out(long_out)),
    )
    text = "\n".join(lines)
    assert lines[0] == f"LANE-TRACE lane={LANE} window={SINCE}..{UNTIL}"
    assert f"lane={LANE}" in lines[1]
    assert "label=ok exit=0" in lines[1]
    assert "    | hello\n    | world" in text
    assert "label=failed exit=1" in text
    assert "    | boom" in text
    assert "label=refused:args exit=0" in text
    assert "label=timeout exit=124" in text
    assert "line 39" in text
    assert "line 0" in text
    assert lines[-1] == "outcomes calls=5 ok=2 failed=1 refused=1 timeout=1 no-output=0"
    assert all(
        ln.startswith(("call ", "    | ", "LANE-TRACE", "outcomes")) for ln in lines
    )


def test_a_call_that_printed_a_refusal_with_exit_zero_is_counted_refused_and_not_ok() -> (
    None
):
    lines = trace(
        row(1, exit_code=0, out=bash_out(REFUSAL)),
        row(2, exit_code=0, out=bash_out("fine")),
    )
    assert lines[-1] == "outcomes calls=2 ok=1 failed=0 refused=1 timeout=0 no-output=0"
    refused = next(ln for ln in lines if "label=refused" in ln)
    assert "exit=0" in refused
    assert "label=ok" not in refused


def test_without_the_rules_a_refusal_with_exit_zero_reads_ok() -> None:
    """The failing control: the refused count above comes from the overlay's refusal rules, not from the trace."""
    empty = ModelToolOutcomeRules(
        refusal_rules=(),
        timeout_markers=(),
        interrupted_is_timeout=True,
        timeout_exit_codes=(),
        exit_code_pattern="^Exit code ([0-9]+)",
    )
    request = ModelLaneTraceRequest(
        lane=LANE, since=SINCE, until=UNTIL, rows=(row(1, out=bash_out(REFUSAL)),)
    )
    result = HandlerHookLaneTrace(HandlerHookToolOutcome(empty)).handle(request)
    assert result.counts is not None
    assert (result.counts.ok, result.counts.refused) == (1, 0)


def test_a_bash_response_prints_stdout_then_stderr_and_other_stored_text_prints_as_it_is() -> (
    None
):
    assert display_output(bash_out("a\nb", "warn")) == ["a", "b", "[stderr]", "warn"]
    assert display_output("Exit code 1\nboom") == ["Exit code 1", "boom"]
    assert display_output(json.dumps([1, 2])) == ["[1, 2]"]


def test_a_lane_with_no_stored_output_shows_calls_without_output_instead_of_a_label() -> (
    None
):
    lines = trace(row(1), row(2, out=bash_out("x")))
    assert "label=no-output" in lines[1]
    assert "    | (no stored output)" in lines[2]
    assert lines[-1] == "outcomes calls=2 ok=1 failed=0 refused=0 timeout=0 no-output=1"


def test_zero_calls_prints_an_outcomes_line_of_zero_calls() -> None:
    assert (
        trace()[-1] == "outcomes calls=0 ok=0 failed=0 refused=0 timeout=0 no-output=0"
    )


def test_a_read_cut_by_the_limit_says_so() -> None:
    lines = trace(row(1, out=bash_out("x")), row(2, out=bash_out("y")), limit=2)
    assert any(ln.startswith("note    newest 2 calls shown") for ln in lines)


def test_a_call_reads_as_the_typed_label_enum() -> None:
    request = ModelLaneTraceRequest(
        lane=LANE, since=SINCE, until=UNTIL, rows=(row(1, out=bash_out(REFUSAL)),)
    )
    call = handler().handle(request).calls[0]
    assert call.label is EnumLaneCallLabel.REFUSED
    assert call.refusing_component == "args"


def test_the_query_joins_the_call_to_its_output_and_reads_only() -> None:
    sql = build_sql(
        ModelLaneTraceRequest(lane=LANE, since=SINCE, until=UNTIL, limit=50)
    )
    assert sql.startswith("BEGIN READ ONLY;")
    assert sql.rstrip().endswith("COMMIT;")
    assert f"e.lane = '{LANE}'" in sql
    assert "omninode_internal.session_content" in sql
    assert "sc.tool_use_id = e.tool_use_id" in sql
    assert "string_agg(sc.content, '' ORDER BY sc.chunk_index)" in sql
    assert f"emitted_at >= '{SINCE}'" in sql
    assert "LIMIT 50" in sql


def test_without_rows_the_result_is_only_the_query() -> None:
    result = handler().handle(
        ModelLaneTraceRequest(lane=LANE, since=SINCE, until=UNTIL)
    )
    assert result.sql.startswith("BEGIN READ ONLY;")
    assert (result.calls, result.counts, result.lines) == ((), None, ())


@pytest.mark.parametrize(
    "lane",
    ["", "a b", "x'; DROP TABLE t;--", "lane\nname", "lane\n", "../etc", "a|b", "ünï"],
)
def test_lane_trace_refuses_a_lane_name_that_is_not_a_plain_token(lane: str) -> None:
    with pytest.raises(ValidationError, match="refuses lane name"):
        ModelLaneTraceRequest(lane=lane, since=SINCE, until=UNTIL)


@pytest.mark.parametrize(
    "bound",
    [
        "",
        "yesterday",
        "2026-11-14",
        "2026-11-14T03:00:00",
        "2026-11-14T03:00:00+01:00",
        "2026-11-14T03:00:00Z'; --",
        "2026-11-14 03:00:00Z",
        "2026-11-14T03:00:00Z\n",
    ],
)
def test_lane_trace_refuses_a_time_bound_that_is_not_a_utc_stamp(bound: str) -> None:
    with pytest.raises(ValidationError, match="refuses start bound"):
        ModelLaneTraceRequest(lane=LANE, since=bound, until=UNTIL)
    with pytest.raises(ValidationError, match="refuses end bound"):
        ModelLaneTraceRequest(lane=LANE, since=SINCE, until=bound)


@pytest.mark.parametrize("limit", [0, -1])
def test_lane_trace_refuses_a_limit_under_one(limit: int) -> None:
    with pytest.raises(ValidationError, match="refuses limit"):
        ModelLaneTraceRequest(lane=LANE, since=SINCE, until=UNTIL, limit=limit)


def _overlay_environ(tmp_path: Path) -> dict[str, str]:
    node_dir = tmp_path / "node_hook_tool_outcome_compute"
    node_dir.mkdir()
    shutil.copy(EXAMPLE, node_dir / "overlay.yaml")
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def _run(
    argv: list[str], stdin: str = "", environ: dict[str, str] | None = None
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, io.StringIO(stdin), out, err, environ or {}, NOW)
    return code, out.getvalue(), err.getvalue()


def test_the_command_line_prints_the_query_for_a_window_ending_now_when_given_no_rows() -> (
    None
):
    code, out, _ = _run(["--lane", LANE, "--hours", "1"])
    assert code == 0
    assert out.startswith("BEGIN READ ONLY;")
    assert f"emitted_at >= '{SINCE}' AND e.emitted_at < '{UNTIL}'" in out


def test_the_command_line_prints_the_trace_of_the_rows_the_query_returned(
    tmp_path: Path,
) -> None:
    lines = [
        "now|2026-11-14 04:30:00+00",
        "tc|" + row(1, out=bash_out(REFUSAL)).model_dump_json(),
        "tc|" + row(2, out=bash_out("fine")).model_dump_json(),
        "tc|" + row(3).model_dump_json(),
    ]
    code, out, _ = _run(
        ["--lane", LANE, "--hours", "1", "--rows", "-"],
        "\n".join(lines) + "\n",
        _overlay_environ(tmp_path),
    )
    assert code == 0
    assert out.splitlines()[0] == f"LANE-TRACE lane={LANE} window={SINCE}..{UNTIL}"
    assert (
        out.splitlines()[-1]
        == "outcomes calls=3 ok=1 failed=0 refused=1 timeout=0 no-output=1"
    )


def test_the_command_line_refuses_a_bad_lane_or_bound_with_exit_2() -> None:
    for argv in (
        ["--lane", "bad lane; drop"],
        ["--lane", LANE, "--since", "last tuesday"],
        ["--lane", LANE, "--until", ""],
    ):
        code, out, err = _run(argv)
        assert code == 2
        assert out == ""
        assert "lane-trace refused" in err


def test_the_command_line_refuses_a_bad_rows_line_with_exit_2(tmp_path: Path) -> None:
    code, _, err = _run(
        ["--lane", LANE, "--rows", "-"],
        'tc|{"emitted_at": 1}\n',
        _overlay_environ(tmp_path),
    )
    assert code == 2
    assert "rows line 1" in err


def test_the_command_line_with_no_overlay_is_indeterminate_not_a_guess(
    tmp_path: Path,
) -> None:
    code, out, err = _run(
        ["--lane", LANE, "--rows", "-"], "", {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    )
    assert code == 3
    assert out == ""
    assert err.startswith("INDETERMINATE:")


def test_the_command_line_reads_rows_from_standard_input_only() -> None:
    code, out, err = _run(["--lane", LANE, "--rows", "some-file.txt"])
    assert code == 2
    assert out == ""
    assert "standard input only" in err
