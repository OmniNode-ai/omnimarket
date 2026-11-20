# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The nightly omnimarket release cut tick.

The release-staleness gate fails a repository whose oldest unreleased packaged commit is over 24
hours old. These tests pin what a tick decides from the release train's row, that only a CUT with
no release-cut run in flight dispatches, that every other train verdict is a named BLOCK that is
raised once per (reason, sha), that the ledger row is a valid ledger row, and that the command
line keeps the surface of the module it replaces.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket.nodes.node_release_cut_tick_effect as node_package
from omnimarket.handlers.ledger_row_grammar import parse_row
from omnimarket.nodes.node_release_cut_tick_effect.handlers import (
    release_cut_decision as decision_module,
)
from omnimarket.nodes.node_release_cut_tick_effect.handlers.handler_release_cut_tick import (
    HandlerReleaseCutTick,
)
from omnimarket.nodes.node_release_cut_tick_effect.handlers.release_cut_decision import (
    ACTION_BLOCKED,
    ACTION_CUT_DISPATCHED,
    ACTION_NOTHING_TO_CUT,
    block_key,
    decide_outcome,
    read_decision,
    should_raise,
    status_row,
)
from omnimarket.nodes.node_release_cut_tick_effect.handlers.release_cut_io import (
    ModelCompleted,
    append_row,
    write_cut_policy,
)
from omnimarket.nodes.node_release_cut_tick_effect.models.model_release_cut_tick import (
    ModelReleaseCutOutcome,
    ModelReleaseCutTickRequest,
    ModelReleaseCutTickResult,
)
from omnimarket.nodes.node_release_cut_tick_effect.runtime_release_cut_tick import (
    LEDGER_VARIABLE,
    main,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
REPO_ROOT = NODE_DIR.parents[3]
NOW = datetime(2026, 9, 29, 13, 0, tzinfo=UTC)
SHA = "a890920d526f3365407f7263040f14549accc674"


def _row(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "repo": "omnimarket",
        "verdict": "CUT",
        "reason": "unreleased_work_lab_proven",
        "detail": "3 unreleased release-relevant commit(s); PASS receipt",
        "base_branch": "dev",
        "candidate_sha": SHA,
        "candidate_version": "0.4.263",
        "latest_tag": "v0.4.262",
        "dev_version": "0.4.263",
        "unreleased_count": 3,
        "needs_bump": False,
    }
    base.update(over)
    return base


def _decide(row: dict[str, Any] | None, **over: Any) -> ModelReleaseCutOutcome:
    kwargs: dict[str, Any] = {
        "decision": row,
        "plan_returncode": 0,
        "plan_stderr": "",
        "age_hours": 2.0,
        "active_runs": [],
        "active_reason": "",
        "previous": ("none", ""),
    }
    kwargs.update(over)
    return decide_outcome(**kwargs)


def test_a_cut_verdict_with_nothing_in_flight_dispatches() -> None:
    outcome = _decide(_row())
    assert outcome.action == ACTION_CUT_DISPATCHED
    assert outcome.candidate_version == "0.4.263"


def test_no_unreleased_work_is_nothing_to_cut() -> None:
    row = _row(
        verdict="SKIP", reason="no_unreleased_release_relevant_work", unreleased_count=0
    )
    assert _decide(row).action == ACTION_NOTHING_TO_CUT


@pytest.mark.parametrize(
    "reason",
    [
        "lab_receipt_absent",
        "lab_receipt_pending",
        "lab_receipt_fail",
        "lab_receipt_indeterminate",
        "lab_receipt_name_payload_disagree",
        "lab_receipt_unreadable",
        "ci_not_green",
        "ci_required_context_pending",
        "mode_report_only",
    ],
)
def test_every_other_skip_reason_is_a_named_block_that_never_cuts(reason: str) -> None:
    outcome = _decide(_row(verdict="SKIP", reason=reason))
    assert outcome.action == ACTION_BLOCKED
    assert outcome.reason == reason


def test_a_refused_train_row_is_a_named_block() -> None:
    outcome = _decide(_row(verdict="REFUSE", reason="version_unreadable"))
    assert (outcome.action, outcome.reason) == (ACTION_BLOCKED, "version_unreadable")


def test_a_missing_train_row_blocks_and_never_defaults_to_a_cut() -> None:
    outcome = _decide(None, plan_returncode=2, plan_stderr="::error::boom")
    assert (outcome.action, outcome.reason) == (
        ACTION_BLOCKED,
        "train_decision_unreadable",
    )
    assert "boom" in outcome.detail


def test_a_dev_version_not_ahead_of_the_tag_blocks_because_release_cut_refuses_it() -> (
    None
):
    outcome = _decide(_row(needs_bump=True, dev_version="0.4.262"))
    assert (outcome.action, outcome.reason) == (
        ACTION_BLOCKED,
        "dev_version_not_ahead_of_tag",
    )


def test_a_release_cut_run_in_flight_waits_and_does_not_dispatch() -> None:
    outcome = _decide(_row(), active_runs=[{"databaseId": 1, "url": "u"}])
    assert (outcome.action, outcome.reason) == (
        ACTION_NOTHING_TO_CUT,
        "release_cut_run_in_flight",
    )


def test_an_unreadable_run_list_blocks_the_dispatch() -> None:
    outcome = _decide(_row(), active_runs=None, active_reason="gh run list failed")
    assert (outcome.action, outcome.reason) == (
        ACTION_BLOCKED,
        "release_cut_runs_unreadable",
    )


def test_a_failed_previous_dispatch_is_a_block_even_with_a_cut_verdict() -> None:
    outcome = _decide(_row(), previous=("failed", "run u concluded failure"))
    assert (outcome.action, outcome.reason) == (
        ACTION_BLOCKED,
        "release_cut_run_failed",
    )


def test_read_decision_finds_the_json_after_the_markdown_table() -> None:
    stdout = "| repo |\n| -- |\n\n**omnimarket** - SKIP\n" + json.dumps(
        {"decisions": [_row(repo="other"), _row()]}, indent=2
    )
    row = read_decision(stdout)
    assert row is not None
    assert row["repo"] == "omnimarket"
    assert read_decision("no json here") is None
    assert read_decision("{\n not json") is None


def test_the_cut_policy_flips_only_omnimarket_and_refuses_a_missing_entry(
    tmp_path: Path,
) -> None:
    source = tmp_path / "policy.yaml"
    source.write_text(
        yaml.safe_dump(
            {
                "repos": {
                    "omnimarket": {"mode": "report_only"},
                    "omnibase_core": {"mode": "cut"},
                }
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out.yaml"
    assert write_cut_policy(source, out) is None
    written = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert written["repos"]["omnimarket"]["mode"] == "cut"
    assert written["repos"]["omnibase_core"]["mode"] == "cut"
    source.write_text(
        yaml.safe_dump({"repos": {"omnibase_core": {}}}), encoding="utf-8"
    )
    reason = write_cut_policy(source, tmp_path / "missing.yaml")
    assert reason is not None
    assert "no omnimarket entry" in reason
    assert not (tmp_path / "missing.yaml").exists()
    assert write_cut_policy(tmp_path / "absent.yaml", tmp_path / "x.yaml") is not None


def test_should_raise_is_edge_triggered_with_one_escalation_past_the_age() -> None:
    blocked = _decide(_row(verdict="SKIP", reason="lab_receipt_fail"), age_hours=3.0)
    assert should_raise(blocked, {})
    state = {"last_block_key": block_key(blocked)}
    assert not should_raise(blocked, state)
    changed = _decide(_row(verdict="SKIP", reason="ci_not_green"), age_hours=3.0)
    assert should_raise(changed, state)
    old = _decide(_row(verdict="SKIP", reason="lab_receipt_fail"), age_hours=19.0)
    assert should_raise(old, state)
    assert not should_raise(old, {**state, "escalated_at_age": True})
    assert not should_raise(_decide(_row()), {})


# ---- one full tick against a fake command runner ------------------------------------------


class _Fake:
    """A command runner keyed on the head of each argv, recording every call."""

    def __init__(
        self, *, verdict: dict[str, Any], runs: list[dict[str, Any]] | None = None
    ) -> None:
        self.verdict = verdict
        self.active = runs or []
        self.calls: list[list[str]] = []
        self.dispatched = False

    def __call__(self, argv: Sequence[str]) -> ModelCompleted:
        cmd = list(argv)
        self.calls.append(cmd)
        joined = " ".join(cmd)
        if cmd[:1] == ["sleep"]:
            return ModelCompleted(returncode=0)
        if "fetch" in cmd or "archive" in cmd:
            return ModelCompleted(returncode=0)
        if cmd[0] == "tar":
            # Unpacking the train leaves the policy the tick then copies with the cut mode.
            config = Path(cmd[cmd.index("-C") + 1]) / "config"
            config.mkdir(parents=True, exist_ok=True)
            (config / "release_train_policy.yaml").write_text(
                yaml.safe_dump({"repos": {"omnimarket": {"mode": "report_only"}}}),
                encoding="utf-8",
            )
            return ModelCompleted(returncode=0)
        if "show" in cmd and "validate_pr_deploy_required.py" in joined:
            return ModelCompleted(returncode=0, stdout="# validator\n")
        if cmd[-3:-1] == ["--", "src"] or "log" in cmd:
            when = (NOW - timedelta(hours=2)).isoformat()
            return ModelCompleted(returncode=0, stdout=f"{when}\tfeat: real work\n")
        if "release_train.py" in joined:
            return ModelCompleted(
                returncode=0,
                stdout="table\n" + json.dumps({"decisions": [self.verdict]}, indent=2),
            )
        if cmd[:3] == ["gh", "run", "list"]:
            if "--status" in cmd:
                return ModelCompleted(returncode=0, stdout=json.dumps(self.active))
            runs = [{"databaseId": 7, "url": "https://x/runs/7"}]
            if self.dispatched:
                runs.append({"databaseId": 8, "url": "https://x/runs/8"})
            return ModelCompleted(returncode=0, stdout=json.dumps(runs))
        if cmd[:3] == ["gh", "workflow", "run"]:
            self.dispatched = True
            return ModelCompleted(returncode=0)
        return ModelCompleted(returncode=1, stderr=f"unexpected command {joined}")


class _World:
    def __init__(self, tmp_path: Path) -> None:
        self.omni = tmp_path / "omni"
        self.omni.mkdir()
        self.ledger = tmp_path / "LEDGER.md"
        self.ledger.write_text("# Ledger\n", encoding="utf-8")
        self.state = tmp_path / "state"
        self.appended: list[tuple[list[str], str]] = []
        self.appender_ok = True
        self.notes: list[str] = []

    def append(
        self, command: Sequence[str], ledger: Path, row: str
    ) -> tuple[bool, str]:
        self.appended.append((list(command), row))
        if not self.appender_ok:
            return False, "rc=1 refused"
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(row + "\n")
        return True, "rc=0"

    def notify(self, osascript: str, title: str, message: str) -> bool:
        self.notes.append(title)
        return True

    def rows(self) -> list[str]:
        return [
            line
            for line in self.ledger.read_text(encoding="utf-8").splitlines()
            if "STATUS" in line
        ]

    def state_file(self, name: str) -> dict[str, Any]:
        return json.loads((self.state / name).read_text(encoding="utf-8"))


@pytest.fixture
def world(tmp_path: Path) -> _World:
    return _World(tmp_path)


def _tick(
    world: _World,
    fake: _Fake | Any,
    now: datetime = NOW,
    *,
    dry_run: bool = False,
    **over: Any,
) -> ModelReleaseCutTickResult:
    handler = HandlerReleaseCutTick(
        run=fake, appender=world.append, notifier=world.notify
    )
    request = ModelReleaseCutTickRequest(
        registry_root=str(world.omni),
        ledger_path=str(world.ledger),
        ledger_command=("onex-ledger",),
        state_dir=str(world.state),
        python="python3",
        osascript="/usr/bin/osascript",
        now=now.isoformat(),
        dry_run=dry_run,
        **over,
    )
    return handler.handle(request)


def test_a_ready_tick_dispatches_release_cut_once_and_records_the_run(
    world: _World,
) -> None:
    fake = _Fake(verdict=_row())
    result = _tick(world, fake)
    assert result.outcome.action == ACTION_CUT_DISPATCHED
    assert result.status == "success"
    assert result.outcome.run_url == "https://x/runs/8"
    dispatches = [c for c in fake.calls if c[:3] == ["gh", "workflow", "run"]]
    assert dispatches == [
        [
            "gh",
            "workflow",
            "run",
            "release-cut.yml",
            "--repo",
            "OmniNode-ai/omnimarket",
            "--ref",
            "dev",
        ]
    ]
    rows = world.rows()
    assert len(rows) == 1
    assert "outcome=cut-dispatched" in rows[0]
    assert "run=https://x/runs/8" in rows[0]
    assert world.appended[0][0] == ["onex-ledger"]
    assert world.state_file("state.json")["dispatched_run_id"] == "8"
    assert world.state_file("last-run.json")["action"] == ACTION_CUT_DISPATCHED


def test_a_blocked_tick_names_the_reason_and_raises_once(world: _World) -> None:
    fake = _Fake(
        verdict=_row(verdict="SKIP", reason="lab_receipt_fail", detail="probe failed")
    )
    first = _tick(world, fake)
    assert first.outcome.action == ACTION_BLOCKED
    assert first.status == "partial"
    assert first.raised
    assert first.notified is True
    assert not [c for c in fake.calls if c[:3] == ["gh", "workflow", "run"]]
    rows = world.rows()
    assert len(rows) == 1
    assert "reason=lab_receipt_fail" in rows[0]
    assert "outcome=blocked" in rows[0]
    assert world.notes == ["omnimarket release blocked: lab_receipt_fail"]
    _tick(world, fake, NOW + timedelta(hours=1))
    assert len(world.rows()) == 1
    assert len(world.notes) == 1
    last = world.state_file("last-run.json")
    assert last["reason"] == "lab_receipt_fail"
    assert last["raised"] is False


def test_a_new_reason_raises_again_and_a_recovery_clears_the_edge(
    world: _World,
) -> None:
    _tick(world, _Fake(verdict=_row(verdict="SKIP", reason="lab_receipt_fail")))
    _tick(
        world,
        _Fake(verdict=_row(verdict="SKIP", reason="ci_not_green")),
        NOW + timedelta(hours=1),
    )
    assert len(world.rows()) == 2
    _tick(
        world,
        _Fake(
            verdict=_row(
                verdict="SKIP",
                reason="no_unreleased_release_relevant_work",
                unreleased_count=0,
            )
        ),
        NOW + timedelta(hours=2),
    )
    assert "last_block_key" not in world.state_file("state.json")
    _tick(
        world,
        _Fake(verdict=_row(verdict="SKIP", reason="ci_not_green")),
        NOW + timedelta(hours=3),
    )
    assert len(world.rows()) == 3


def test_a_quiet_tick_writes_a_run_record_and_no_row(world: _World) -> None:
    fake = _Fake(
        verdict=_row(
            verdict="SKIP",
            reason="no_unreleased_release_relevant_work",
            unreleased_count=0,
        )
    )
    result = _tick(world, fake)
    assert result.outcome.action == ACTION_NOTHING_TO_CUT
    assert world.rows() == []
    last = world.state_file("last-run.json")
    assert last["ran_at"] == "2026-09-29T13:00:00Z"
    assert last["action"] == ACTION_NOTHING_TO_CUT


def test_the_release_cut_run_in_flight_is_not_redispatched(world: _World) -> None:
    fake = _Fake(verdict=_row(), runs=[{"databaseId": 9, "url": "https://x/runs/9"}])
    result = _tick(world, fake)
    assert result.outcome.reason == "release_cut_run_in_flight"
    assert not [c for c in fake.calls if c[:3] == ["gh", "workflow", "run"]]


def test_dry_run_dispatches_nothing_and_writes_nothing(world: _World) -> None:
    fake = _Fake(verdict=_row())
    result = _tick(world, fake, dry_run=True)
    assert result.outcome.action == ACTION_CUT_DISPATCHED
    assert result.dry_run
    assert result.status_row == ""
    assert not [c for c in fake.calls if c[:3] == ["gh", "workflow", "run"]]
    assert world.rows() == []
    assert not world.state.exists()
    assert world.notes == []


def test_a_failed_fetch_blocks_and_never_decides_on_a_stale_tree(
    world: _World,
) -> None:
    class _Broken(_Fake):
        def __call__(self, argv: Sequence[str]) -> ModelCompleted:
            if "fetch" in argv:
                return ModelCompleted(returncode=1, stderr="network down")
            return super().__call__(argv)

    result = _tick(world, _Broken(verdict=_row()))
    assert (result.outcome.action, result.outcome.reason) == (
        ACTION_BLOCKED,
        "train_inputs_unavailable",
    )
    assert "network down" in result.outcome.detail


def test_the_dispatched_run_verdict_is_read_once_and_a_failure_blocks(
    world: _World,
) -> None:
    fake = _Fake(verdict=_row())
    _tick(world, fake)

    def _view(argv: Sequence[str]) -> ModelCompleted:
        if list(argv)[:3] == ["gh", "run", "view"]:
            return ModelCompleted(
                returncode=0,
                stdout=json.dumps(
                    {
                        "status": "completed",
                        "conclusion": "failure",
                        "url": "https://x/runs/8",
                    }
                ),
            )
        return fake(argv)

    result = _tick(world, _view, NOW + timedelta(hours=1))
    assert (result.outcome.action, result.outcome.reason) == (
        ACTION_BLOCKED,
        "release_cut_run_failed",
    )
    assert world.state_file("state.json")["dispatched_run_checked"] is True
    after = _tick(world, fake, NOW + timedelta(hours=2))
    assert after.outcome.reason != "release_cut_run_failed"


def test_a_refused_append_does_not_record_the_edge_as_seen(world: _World) -> None:
    fake = _Fake(verdict=_row(verdict="SKIP", reason="lab_receipt_fail"))
    world.appender_ok = False
    first = _tick(world, fake)
    assert first.error_message.startswith("ledger append failed")
    assert not first.raised
    assert world.state_file("last-run.json")["raised"] is False
    assert "last_block_key" in world.state_file("state.json")
    assert world.state_file("state.json")["last_block_key"] is None
    world.appender_ok = True
    _tick(world, fake, NOW + timedelta(hours=1))
    assert len(world.rows()) == 1


def test_a_ledger_lock_script_is_run_with_the_python_in_front(world: _World) -> None:
    _tick(
        world,
        _Fake(verdict=_row(verdict="SKIP", reason="lab_receipt_fail")),
        ledger_lock="lock.py",
    )
    assert world.appended[0][0] == ["python3", "lock.py"]


@pytest.mark.parametrize("reason", ["lab_receipt_fail", "release_cut_dispatch_failed"])
def test_the_row_is_a_valid_ledger_status_row(reason: str) -> None:
    outcome = ModelReleaseCutOutcome(
        action="blocked",
        reason=reason,
        detail="the dev lane recorded FAIL | pipes and\nnewlines are flattened",
        candidate_sha=SHA,
        candidate_version="0.4.263",
        latest_tag="v0.4.262",
        unreleased_count=3,
        oldest_unreleased_age_hours=19.2,
    )
    row = status_row(outcome, NOW, escalated=True)
    assert "\n" not in row
    assert row.count(" | ") >= 10
    parsed = parse_row(row)
    assert parsed is not None
    assert parsed.row_type == "STATUS"
    assert parsed.token("lane") == "omnimarket-release-cut-timer"
    assert parsed.value("reason") == reason
    assert parsed.value("flag") == "escalated-past-18h"
    assert parsed.value("ticket") is None


def test_the_ticket_cell_is_in_the_row_only_when_one_is_given() -> None:
    outcome = ModelReleaseCutOutcome(action="blocked", reason="ci_not_green")
    row = status_row(outcome, NOW, escalated=False, ticket="PROJ-1")
    parsed = parse_row(row)
    assert parsed is not None
    assert parsed.value("ticket") == "PROJ-1"


def test_the_ledger_command_appends_through_a_real_subprocess(tmp_path: Path) -> None:
    ledger = tmp_path / "LEDGER.md"
    ledger.write_text("# Ledger\n", encoding="utf-8")
    script = tmp_path / "lock.py"
    script.write_text(
        "import sys\nopen(sys.argv[1], 'a').write(sys.argv[3] + '\\n')\n",
        encoding="utf-8",
    )
    ok, detail = append_row([sys.executable, str(script)], ledger, "one row")
    assert ok
    assert detail.startswith("rc=0")
    assert ledger.read_text(encoding="utf-8").splitlines()[-1] == "one row"


def test_no_ledger_command_refuses_to_append_and_names_the_variable(
    tmp_path: Path,
) -> None:
    ok, detail = append_row([], tmp_path / "LEDGER.md", "row")
    assert not ok
    assert "ONEX_LEDGER_COMMAND" in detail


# ---- the command line ---------------------------------------------------------------------


def test_omni_home_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNI_HOME", raising=False)
    assert main(["--dry-run"]) == 2


def test_the_ledger_path_is_required_without_a_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.delenv(LEDGER_VARIABLE, raising=False)
    assert main(["--dry-run"]) == 2
    monkeypatch.setenv(LEDGER_VARIABLE, "relative/LEDGER.md")
    assert main(["--dry-run"]) == 2


def test_the_command_line_runs_a_dry_run_tick_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(LEDGER_VARIABLE, str(tmp_path / "LEDGER.md"))
    omni = tmp_path / "omni"
    omni.mkdir()
    code = main(
        ["--omni-home", str(omni), "--now", "2026-09-29T13:00:00Z", "--dry-run"]
    )
    out = capsys.readouterr().out
    # The clones are absent, so the fetch fails and the tick names the block.
    assert code == 0
    assert "omnimarket-release-cut: blocked reason=train_inputs_unavailable" in out
    assert not (omni / ".onex_state").exists()


def test_the_argparse_surface_is_the_one_the_scheduled_unit_passes() -> None:
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_release_cut_tick_effect",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    for flag in (
        "--omni-home",
        "--ledger",
        "--ledger-lock",
        "--state-dir",
        "--python",
        "--osascript",
        "--now",
        "--dry-run",
    ):
        assert flag in done.stdout


# ---- the node's contract ------------------------------------------------------------------


def test_the_contract_names_the_handler_and_declares_its_topics() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["name"] == "node_release_cut_tick_effect"
    assert contract["node_type"] == "effect"
    assert contract["handler"]["class"] == "HandlerReleaseCutTick"
    # A scheduled unit runs this node, so no shared runtime attaches it over the broker.
    assert "event_bus" not in contract
    assert contract["runtime_dispatch"]["command_topic"].startswith("onex.cmd.")
    assert contract["terminal_event"].startswith("onex.evt.")


def test_the_handlers_hold_no_topic_string() -> None:
    for path in (NODE_DIR / "handlers").glob("*.py"):
        assert "onex.cmd." not in path.read_text(encoding="utf-8")
        assert "onex.evt." not in path.read_text(encoding="utf-8")


def test_the_node_is_registered_under_the_node_entry_points() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    nodes = project["project"]["entry-points"]["onex.nodes"]
    assert (
        nodes["node_release_cut_tick_effect"]
        == "omnimarket.nodes.node_release_cut_tick_effect"
    )


def test_the_node_does_not_overlap_the_org_wide_release_pipeline() -> None:
    other = yaml.safe_load(
        (NODE_DIR.parent / "node_release" / "contract.yaml").read_text(encoding="utf-8")
    )
    assert other["node_type"] == "orchestrator"
    assert decision_module.REPO == "omnimarket"
    assert decision_module.WORKFLOW == "release-cut.yml"
