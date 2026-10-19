# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: a dev cause-lane request starts one lane, once per dispatch key.

Ruling 2026-10-14T03:57:46Z (item 5): a component alarm starts a fix lane automatically, one per
cause. The orchestrator asks; these tests pin the receiver that starts it. Each guarantee has a
negative control: a ledger that forgets, and a receiver that acts while act is false.
"""

from __future__ import annotations

import json
import stat
import sys
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from omnimarket.events.topics import DEV_CAUSE_LANE_REQUESTED_TOPIC_V1
from omnimarket.models.dev_head_red import (
    ModelDevCauseLaneRequest,
    dev_cause_dispatch_key,
    dev_cause_lane_name,
    dev_cause_ref,
    dev_cause_request_correlation_id,
    dev_head_owner_key,
)
from omnimarket.nodes.node_dev_cause_lane_receiver_effect.handlers import (
    HandlerDevCauseLaneStart,
)
from omnimarket.nodes.node_dev_cause_lane_receiver_effect.models import (
    EnumDevCauseLaneStartOutcome,
    ModelRunnerReceipt,
)
from omnimarket.nodes.node_dev_cause_lane_receiver_effect.protocols.local_dev_cause_lane_adapters import (
    FileDispatchKeyLedger,
    SubprocessCauseLaneStarter,
)

pytestmark = pytest.mark.unit

NAME = "node_dev_cause_lane_receiver_effect"
OUTCOME = EnumDevCauseLaneStartOutcome


def _request(
    head_sha: str = "a" * 40, check: str = "CI Summary"
) -> ModelDevCauseLaneRequest:
    repo, branch = "OmniNode-ai/omnimarket", "dev"
    owner_key = dev_head_owner_key(repo, branch, check)
    dispatch_key = dev_cause_dispatch_key(owner_key, head_sha)
    return ModelDevCauseLaneRequest(
        correlation_id=dev_cause_request_correlation_id(dispatch_key),
        dispatch_key=dispatch_key,
        owner_key=owner_key,
        lane=dev_cause_lane_name(repo, dispatch_key),
        repo=repo,
        branch=branch,
        head_sha=head_sha,
        check=check,
        cause=dev_cause_ref(repo, check),
        event_id="b" * 64,
        observed_at="2026-10-18T17:00:00Z",
    )


class _Starter:
    def __init__(self, started: bool = True) -> None:
        self.started = started
        self.calls: list[tuple[str, str]] = []

    def start(
        self, request: ModelDevCauseLaneRequest, brief: str
    ) -> ModelRunnerReceipt:
        self.calls.append((request.lane, brief))
        return ModelRunnerReceipt(
            started=self.started, detail="rc=0" if self.started else "rc=3 boom"
        )


class _ForgetfulLedger:
    """The negative control: a ledger whose memory is lost, so every request is a first."""

    def claim(self, dispatch_key: str, lane: str) -> bool:
        return True

    def record(self, dispatch_key: str, outcome: str, detail: str) -> None:
        return None


def _handler(
    tmp_path: Path, runner: _Starter, *, act: bool = True
) -> HandlerDevCauseLaneStart:
    return HandlerDevCauseLaneStart(
        act=act, ledger=FileDispatchKeyLedger(tmp_path), runner=runner
    )


def test_a_request_starts_one_lane_with_its_cause_in_the_brief(tmp_path: Path) -> None:
    runner = _Starter()
    result = _handler(tmp_path, runner).handle(_request())
    assert result.outcome is OUTCOME.STARTED
    assert len(runner.calls) == 1
    lane, brief = runner.calls[0]
    assert lane == _request().lane
    assert "omnimarket:CI Summary" in brief
    assert brief.startswith("ROUTE: ")


def test_a_redelivered_request_starts_no_second_lane(tmp_path: Path) -> None:
    runner = _Starter()
    handler = _handler(tmp_path, runner)
    first = handler.handle(_request())
    second = handler.handle(_request())
    assert (first.outcome, second.outcome) == (OUTCOME.STARTED, OUTCOME.DUPLICATE)
    assert len(runner.calls) == 1


def test_a_restart_does_not_forget_the_key(tmp_path: Path) -> None:
    runner = _Starter()
    _handler(tmp_path, runner).handle(_request())
    after_restart = _handler(tmp_path, runner).handle(_request())
    assert after_restart.outcome is OUTCOME.DUPLICATE
    assert len(runner.calls) == 1


def test_negative_control_a_forgetful_ledger_starts_two_lanes() -> None:
    runner = _Starter()
    handler = HandlerDevCauseLaneStart(
        act=True, ledger=_ForgetfulLedger(), runner=runner
    )
    handler.handle(_request())
    handler.handle(_request())
    assert len(runner.calls) == 2


def test_a_new_head_is_a_new_cause_and_starts_its_own_lane(tmp_path: Path) -> None:
    runner = _Starter()
    handler = _handler(tmp_path, runner)
    handler.handle(_request("a" * 40))
    result = handler.handle(_request("c" * 40))
    assert result.outcome is OUTCOME.STARTED
    assert len(runner.calls) == 2


def test_a_failed_start_keeps_the_key_and_says_so(tmp_path: Path) -> None:
    runner = _Starter(started=False)
    handler = _handler(tmp_path, runner)
    first = handler.handle(_request())
    again = handler.handle(_request())
    assert first.outcome is OUTCOME.FAILED
    assert "boom" in first.detail
    assert again.outcome is OUTCOME.DUPLICATE
    kept = json.loads(
        (tmp_path / "dispatch-keys" / f"{_request().dispatch_key}.json").read_text()
    )
    assert (kept["outcome"], "boom" in kept["detail"]) == ("failed", True)


def test_act_false_answers_dry_run_and_takes_no_key(tmp_path: Path) -> None:
    runner = _Starter()
    result = _handler(tmp_path, runner, act=False).handle(_request())
    assert result.outcome is OUTCOME.DRY_RUN
    assert runner.calls == []
    assert not list((tmp_path / "dispatch-keys").glob("*.json"))
    enabled = _handler(tmp_path, runner, act=True).handle(_request())
    assert enabled.outcome is OUTCOME.STARTED


def test_the_shipped_contract_does_not_act(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEX_DEV_CAUSE_LANE_RECEIVER_ACT", raising=False)
    assert HandlerDevCauseLaneStart().handle(_request()).outcome is OUTCOME.DRY_RUN


def test_acting_without_the_host_environment_fails_at_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_DEV_CAUSE_LANE_STATE_DIR", raising=False)
    with pytest.raises(ValueError, match="ONEX_DEV_CAUSE_LANE_STATE_DIR"):
        HandlerDevCauseLaneStart(act=True)


def test_the_subprocess_runner_passes_the_cause_and_detaches(tmp_path: Path) -> None:
    script = tmp_path / "fake_runner.py"
    script.write_text(
        "import json, sys\n"
        f"open({str(tmp_path / 'argv.json')!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        "sys.exit(0)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    runner = SubprocessCauseLaneStarter(script, "OMN-1", tmp_path / "briefs")
    receipt = runner.start(_request(), "ROUTE: x\n")
    argv = json.loads((tmp_path / "argv.json").read_text())
    assert receipt.started
    assert argv[:1] == ["run"]
    assert argv[argv.index("--cause") + 1] == "omnimarket:CI Summary"
    assert argv[argv.index("--lane") + 1] == _request().lane
    assert argv[argv.index("--ticket") + 1] == "OMN-1"
    assert "--detach" in argv
    assert (tmp_path / "briefs" / f"{_request().lane}.md").read_text() == "ROUTE: x\n"


def test_the_subprocess_runner_reports_a_nonzero_exit_and_a_missing_runner(
    tmp_path: Path,
) -> None:
    failing = tmp_path / "failing_runner.py"
    failing.write_text("import sys\nprint('refused', file=sys.stderr)\nsys.exit(3)\n")
    receipt = SubprocessCauseLaneStarter(failing, "OMN-1", tmp_path / "briefs").start(
        _request(), "b"
    )
    assert not receipt.started
    assert "rc=3" in receipt.detail
    assert "refused" in receipt.detail
    missing = SubprocessCauseLaneStarter(
        tmp_path / "none.py", "OMN-1", tmp_path / "briefs"
    )
    assert not missing.start(_request(), "b").started


def _contract() -> dict:
    return yaml.safe_load(
        (files("omnimarket.nodes") / NAME / "contract.yaml").read_text("utf-8")
    )


def test_contract_takes_the_orchestrators_request_topic_and_names_its_handler() -> None:
    contract = _contract()
    assert (
        contract["runtime_dispatch"]["command_topic"]
        == DEV_CAUSE_LANE_REQUESTED_TOPIC_V1
    )
    assert "event_bus" not in contract
    module = contract["handler"]["module"]
    assert module.endswith("handler_dev_cause_lane_start")
    assert sys.modules.get(module) or __import__(module)


_ROOT = Path(__file__).resolve().parents[3]
_SUITE = "tests/nodes/node_dev_cause_lane_receiver_effect"


def test_pre_commit_hook_runs_the_suite_check_only() -> None:
    config = yaml.safe_load((_ROOT / ".pre-commit-config.yaml").read_text("utf-8"))
    hooks = [
        h
        for repo in config["repos"]
        for h in repo["hooks"]
        if h["id"] == "dev-cause-lane-receiver"
    ]
    assert len(hooks) == 1
    assert _SUITE in hooks[0]["entry"]
    assert hooks[0]["pass_filenames"] is False
    assert "pre-commit" in hooks[0]["stages"]
    assert "handler_dev_cause_lane_start.py" in (
        _ROOT / ".pre-commit-config.yaml"
    ).read_text("utf-8")


def test_required_lint_job_runs_the_suite() -> None:
    workflow = yaml.safe_load(
        (_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    )
    runs = [str(step.get("run", "")) for step in workflow["jobs"]["lint"]["steps"]]
    assert any(_SUITE in run and "pytest" in run for run in runs)
