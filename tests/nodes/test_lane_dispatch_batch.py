# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Batch dispatch regressions from refused briefs, busy hosts and lost sends."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_lane_dispatch_effect import runtime_lane_dispatch_cli as cli
from omnimarket.nodes.node_lane_dispatch_effect.handlers.handler_lane_dispatch import (
    HandlerLaneDispatch,
)
from omnimarket.nodes.node_lane_dispatch_effect.models.model_lane_dispatch_effect import (
    ModelLaneDispatchRequest,
    ModelLaneDispatchResult,
)
from tests.nodes.test_lane_dispatch_contract import (
    configure_runtime,
    effect_overlay,
    spec_data,
)


class Commands:
    def __init__(self, *answers: tuple[int, str] | Exception) -> None:
        self.answers = iter(answers)
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str], timeout_s: int) -> tuple[int, str]:
        self.calls.append(tuple(argv))
        assert timeout_s > 0
        answer = next(self.answers)
        if isinstance(answer, Exception):
            raise answer
        return answer


def batch(tmp_path: Path, commands: Commands, *specs: object):
    return HandlerLaneDispatch(
        effect_overlay(), commands, lambda: datetime(2026, 11, 4, 16, 1, tzinfo=UTC)
    ).handle(
        ModelLaneDispatchRequest(
            record_json=json.dumps(list(specs)), output_dir=str(tmp_path), execute=True
        )
    )


def placed(lane: str = "dispatch-proof") -> tuple[int, str]:
    return 0, f"PLACEMENT lane={lane} status=running host=h053 receipt=/run/job.json"


@pytest.mark.parametrize(
    ("answers", "status", "reason", "calls"),
    [
        (((3, "brief refused: merged ticket"),), "refused", "merged ticket", 1),
        (
            (
                (0, "brief accepted"),
                (
                    75,
                    "PLACEMENT lane=dispatch-proof status=no-host reason=host over swap bar receipt=/run/job.json",
                ),
            ),
            "refused",
            "swap bar",
            2,
        ),
        (
            (
                (0, "brief accepted"),
                (
                    70,
                    "PLACEMENT lane=dispatch-proof status=pr-ownership-refused host=none reason=PR held by another owner receipt=/run/job.json",
                ),
            ),
            "refused",
            "another owner",
            2,
        ),
        (
            (
                (0, "brief accepted"),
                (
                    70,
                    "PLACEMENT lane=dispatch-proof status=dead receipt=/run/job.json: the runner exited before placement",
                ),
            ),
            "crashed",
            "exited before placement",
            2,
        ),
        (((0, "brief accepted"), placed()), "placed", "status=running", 2),
        (
            (
                (0, "brief accepted"),
                (
                    3,
                    "PLACEMENT lane=dispatch-proof status=placing after 30s; read it with: onex-remote-lane wait --receipt /run/job.json",
                ),
            ),
            "placing",
            "status=placing",
            2,
        ),
    ],
)
def test_recorded_outcomes_are_never_reported_as_sent(
    tmp_path: Path, answers: tuple, status: str, reason: str, calls: int
) -> None:
    commands = Commands(*answers)
    result = batch(tmp_path, commands, spec_data())
    assert len(result.results) == 1
    outcome = result.results[0]
    assert outcome.status == status
    assert outcome.lane == "dispatch-proof"
    assert outcome.index == 0
    assert reason in outcome.reason
    assert len(commands.calls) == calls
    if calls == 2:
        assert "--detach" in commands.calls[1]
        assert "--no-placement-wait" not in commands.calls[1]
        assert "--placement-wait-s" in commands.calls[1]


@pytest.mark.parametrize("field", ["repo", "parent"])
def test_missing_identity_is_refused_before_artifacts_or_commands(
    tmp_path: Path, field: str
) -> None:
    spec = spec_data()
    del spec[field]
    commands = Commands()
    result = batch(tmp_path, commands, spec)
    assert result.results[0].status == "refused"
    assert field in result.results[0].reason
    assert commands.calls == []
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "crash",
    [
        OSError("runner absent"),
        RuntimeError("crash before send"),
        subprocess.TimeoutExpired("runner", 30),
    ],
)
def test_crash_is_isolated_and_next_lane_is_attempted(
    tmp_path: Path, crash: Exception
) -> None:
    second = spec_data()
    second.update(lane="second", job_id="lj-fedcba9876543210")
    second["constraints"] = second["constraints"].replace(
        "lane=dispatch-proof", "lane=second"
    )
    commands = Commands(
        (0, "brief accepted"), crash, (0, "brief accepted"), placed("second")
    )
    result = batch(tmp_path, commands, spec_data(), second)
    assert [outcome.status for outcome in result.results] == ["crashed", "placed"]
    assert [outcome.index for outcome in result.results] == [0, 1]
    assert [outcome.lane for outcome in result.results] == ["dispatch-proof", "second"]
    assert result.results[0].reason
    assert len(commands.calls) == 4


@pytest.mark.parametrize(
    "output",
    [
        "",
        "DETACHED lane=dispatch-proof pid=123 receipt=/run/job.json",
        "PLACEMENT lane=another status=running host=h053 receipt=/run/other.json",
    ],
)
def test_zero_without_matching_placement_is_crashed(
    tmp_path: Path, output: str
) -> None:
    commands = Commands((0, "brief accepted"), (0, output))
    assert batch(tmp_path, commands, spec_data()).results[0].status == "crashed"


def test_invalid_item_does_not_hide_a_valid_lane(tmp_path: Path) -> None:
    commands = Commands((0, "brief accepted"), placed())
    result = batch(tmp_path, commands, None, spec_data())
    assert [outcome.status for outcome in result.results] == ["refused", "placed"]
    assert result.results[0].index == 0


def test_batch_requires_execution_intent_before_any_command(tmp_path: Path) -> None:
    commands = Commands()
    result = HandlerLaneDispatch(effect_overlay(), commands).handle(
        ModelLaneDispatchRequest(
            record_json=json.dumps([spec_data()]), output_dir=str(tmp_path)
        )
    )
    assert result.results[0].status == "refused"
    assert result.results[0].error_code == "DISPATCH-EXECUTION-REQUIRED"
    assert commands.calls == []
    assert not list(tmp_path.iterdir())


def test_checker_refusal_without_output_still_has_a_reason(tmp_path: Path) -> None:
    commands = Commands((3, ""))
    outcome = batch(tmp_path, commands, spec_data()).results[0]
    assert outcome.status == "refused"
    assert "exit 3" in outcome.reason
    assert len(commands.calls) == 1


def test_no_receipt_is_never_placed(tmp_path: Path) -> None:
    commands = Commands(
        (0, "brief accepted"),
        (0, "PLACEMENT lane=dispatch-proof status=running host=h053 receipt=none"),
    )
    assert batch(tmp_path, commands, spec_data()).results[0].status == "crashed"


@pytest.mark.parametrize(
    ("runner_output", "runner_code", "expected_status", "expected_exit"),
    [
        (placed()[1], 0, "placed", 0),
        ("PLACEMENT lane=dispatch-proof status=placing after 30s", 3, "placing", 3),
        ("host over swap bar", 75, "refused", 65),
        (
            "PLACEMENT lane=dispatch-proof status=pr-ownership-refused host=none reason=PR held by another owner receipt=/run/job.json",
            70,
            "refused",
            65,
        ),
        ("crash before send", 70, "crashed", 70),
        ("DETACHED lane=dispatch-proof pid=123", 0, "crashed", 70),
    ],
)
def test_list_cli_uses_declared_bus_node_and_reports_exit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    runner_output: str,
    runner_code: int,
    expected_status: str,
    expected_exit: int,
) -> None:
    request = configure_runtime(tmp_path, monkeypatch)
    with (tmp_path / "runner.py").open("a") as stream:
        stream.write(f"print({runner_output!r})\nraise SystemExit({runner_code})\n")
    spec_file = tmp_path / "specs.json"
    spec_file.write_text(json.dumps([json.loads(request.record_json)]))
    code = cli.main(
        ["--spec-file", str(spec_file), "--output-dir", request.output_dir, "--execute"]
    )
    result = ModelLaneDispatchResult.model_validate_json(capsys.readouterr().out)
    assert result.status == "batch"
    assert result.results[0].status == expected_status
    assert code == expected_exit
    observed = json.loads((tmp_path / "runner-observed.json").read_text())
    assert "--placement-wait-s" in observed
    assert "--no-placement-wait" not in observed
