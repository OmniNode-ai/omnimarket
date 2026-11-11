# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scenario R8 tick the registry installer renders.

The tick runs the scenario once as a SCHEDULE run. The probe tenant's key is filled from the node's settings
overlay through the secret store, while the tenant's address still comes from the environment file. The golden
chain does it with an injected build that returns a success result: exit 0, the record is success, and stdout
holds the result. The error chains are the named breaks: a build whose run raises RuntimeError refuses with 65
and the record says refused, a failing conclusion exits 1, and ``--help`` exits 0.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from omnimarket.models.model_delegation_scenario import (
    EnumScenarioConclusion,
    EnumScenarioTrigger,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.runtime_scenario_r8_tick import (
    main,
)

pytestmark = pytest.mark.unit


class _Result:
    """The result seam for the tick: the conclusion and the JSON it dumps."""

    def __init__(self, conclusion: EnumScenarioConclusion) -> None:
        self.conclusion = conclusion

    def model_dump_json(self, indent: int | None = None) -> str:
        return json.dumps({"conclusion": self.conclusion.value})


class _Runner:
    """The runner seam for the tick: the result it returns, or the break it raises."""

    def __init__(
        self, result: _Result | None = None, *, raise_msg: str | None = None
    ) -> None:
        self._result = result
        self._raise_msg = raise_msg

    def run(self) -> _Result:
        if self._raise_msg is not None:
            raise RuntimeError(self._raise_msg)
        assert self._result is not None
        return self._result


def _build_for(result: _Result | None, *, raise_msg: str | None = None):
    def build(environment: Mapping[str, str], trigger: EnumScenarioTrigger) -> _Runner:
        assert trigger is EnumScenarioTrigger.SCHEDULE
        return _Runner(result, raise_msg=raise_msg)

    return build


def _record_text(state_dir: Path) -> str:
    return (state_dir / "last-run.json").read_text(encoding="utf-8")


def _tick(tmp_path: Path, build) -> tuple[int, Path]:
    state_dir = tmp_path / "state"
    exit_code = main(
        ["--omni-home", str(tmp_path / "home"), "--state-dir", str(state_dir)],
        build=build,
    )
    return exit_code, state_dir


def test_golden_tick_runs_and_writes_the_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    build = _build_for(_Result(EnumScenarioConclusion.SUCCESS))
    exit_code, state_dir = _tick(tmp_path, build)
    assert exit_code == 0
    record = json.loads(_record_text(state_dir))
    assert record["scenario"] == "R8"
    assert record["conclusion"] == "success"
    assert "ran_at" in record
    captured = capsys.readouterr()
    assert json.loads(captured.out)["conclusion"] == "success"


def test_tick_a_build_that_raises_refuses_with_65(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    build = _build_for(None, raise_msg="the run refused")
    exit_code, state_dir = _tick(tmp_path, build)
    assert exit_code == 65
    record = json.loads(_record_text(state_dir))
    assert record["conclusion"] == "refused"
    assert record["refused"] == "the run refused"
    captured = capsys.readouterr()
    assert "the run refused" in captured.err


def test_tick_a_failing_conclusion_exits_one(tmp_path: Path) -> None:
    build = _build_for(_Result(EnumScenarioConclusion.FAILURE))
    exit_code, state_dir = _tick(tmp_path, build)
    assert exit_code == 1
    record = json.loads(_record_text(state_dir))
    assert record["conclusion"] == "failure"


def test_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--help"])
    assert caught.value.code == 0
