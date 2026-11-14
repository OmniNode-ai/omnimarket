# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A drift lane's brief is completed through the lane-brief path and passes its check before the runner sees it (OMN-24239).

2026-11-13T20:11Z on h053: the live pass read 2 drift items, rendered 2 lane rows and the remote-lane runner refused both
placements with ``REMOTE-LANE-BRIEF-UNCHECKED``, because the node wrote a plain brief that no passing check had stamped.

Failing controls: a brief the check refuses is never handed to the runner and is not recorded (the pass before this change
handed it over and the runner refused it); a brief whose bytes were never stamped is refused by the runner. Seam (drift row
-> checked brief -> runner): the bytes the check stamped are the bytes at the path the runner is given. The runtime preparer
runs real subprocesses against stand-ins for the lane-brief scripts and the runner's stamp read.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired

import pytest

from omnimarket.nodes.node_host_drift_owner_dispatch_effect.adapters.inmemory_adapters import (
    InMemoryBriefPreparer,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.adapters.runtime_adapters import (
    LaneBriefPreparer,
    SubprocessLanePlacer,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.handlers.handler_host_drift_owner_dispatch import (
    HandlerHostDriftOwnerDispatch,
    render_brief,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.models import (
    ModelBriefPrepared,
    ModelDriftOwnerRequest,
)

from .support import CLAUDE_DIR, NOW, RUNNER_PATH, item, overlay, rig

pytestmark = pytest.mark.unit

Run = Callable[..., CompletedProcess[str]]

CAUSE = "host-drift:h-example:env-file:host-env"
LEDGER = "/example/ledger.md"

# A stand-in for the lane-brief script: ticket-text, constraints and check, with the real check's exit codes (0 pass, 3
# refused) and its stamp keyed by the sha256 of the brief's bytes under STAMP_DIR.
LANE_BRIEF = """
import hashlib, os, pathlib, sys
args = sys.argv[1:]
sub = args[args.index("--ledger") + 2]
if sub == "ticket-text":
    print("ticket-text: OMN-1 host drift")
elif sub == "constraints":
    print("ROUTE: band=B3 score=9 dims=S2A1R1V2N1C1J1")
    print("## 3a. Constraints")
elif sub == "check":
    data = pathlib.Path(args[args.index("check") + 1]).read_bytes()
    text = data.decode()
    problems = [
        line for line, ok in (
            ("REFUSED the standing rules block was not pasted", "## 3. Rules" in text),
            ("REFUSED no section 3a", "## 3a. Constraints" in text),
            ("REFUSED no EXEMPLARS line", "EXEMPLARS:" in text),
        ) if not ok
    ]
    if problems:
        print("\\n".join(problems))
        sys.exit(3)
    stamp = pathlib.Path(os.environ["STAMP_DIR"])
    stamp.mkdir(parents=True, exist_ok=True)
    (stamp / (hashlib.sha256(data).hexdigest() + ".json")).write_text("{}")
"""
RULES = "print('# Standing rules for every dispatched lane')"
# A stand-in for the runner's stamp read (OMN-21825): refuse, exit 2, a brief whose bytes carry no stamp.
RUNNER = """
import hashlib, os, pathlib, sys
args = sys.argv[1:]
data = pathlib.Path(args[args.index("--brief") + 1]).read_bytes()
stamp = pathlib.Path(os.environ["STAMP_DIR"]) / (hashlib.sha256(data).hexdigest() + ".json")
if not stamp.is_file():
    print("REMOTE-LANE-BRIEF-UNCHECKED REFUSED", file=sys.stderr)
    sys.exit(2)
"""


def _preparer(
    tmp_path: Path, *, run: Run | None = None, ledger: str | None = LEDGER
) -> LaneBriefPreparer:
    root = tmp_path / "plugin"
    (root / "skills/lane-brief/scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "skills/lane-brief/scripts/lane_brief.py").write_text(LANE_BRIEF)
    (root / "scripts/lane_rules_block.py").write_text(RULES)
    environ = {"EXAMPLE_LEDGER_PATH": ledger, "STAMP_DIR": str(tmp_path / "stamps")}
    return LaneBriefPreparer(
        tmp_path / "briefs",
        root,
        overlay().brief,
        model="sonnet",
        ticket="OMN-1",
        environ={k: v for k, v in environ.items() if v is not None},
        run=run if run is not None else subprocess.run,
    )


def _task() -> str:
    return render_brief(
        item(),
        CAUSE,
        "host-drift-x-20261113T1000Z",
        "2026-11-13T16:00:00Z",
        overlay().brief.sections,
    )


def test_the_task_carries_the_overlays_exemplar_seam_runtime_and_delegation_lines() -> (
    None
):
    task = _task()
    for line in (
        overlay().brief.sections.exemplars,
        overlay().brief.sections.seams,
        overlay().brief.sections.runtime,
        overlay().brief.sections.delegation,
    ):
        assert line in task


def test_a_brief_the_check_refuses_never_reaches_the_runner_and_is_not_recorded() -> (
    None
):
    r = rig((item(),))
    r.briefs.refuse = "REFUSED the standing rules block was not pasted"
    first = r.handler.handle(ModelDriftOwnerRequest(dry_run=False))
    (lane,) = first.lanes
    assert not lane.brief_checked
    assert not lane.started
    assert lane.detail.startswith("brief-check-refused: REFUSED the standing rules")
    assert r.runner.placed == []
    assert r.record.record == {}

    r.briefs.refuse = None
    retry = r.handler.handle(ModelDriftOwnerRequest(dry_run=False))
    assert [(x.brief_checked, x.started) for x in retry.lanes] == [(True, True)]
    assert set(r.record.record) == {CAUSE}


def test_a_dry_run_runs_the_check_and_names_the_stamp_it_left() -> None:
    r = rig((item(),))
    (lane,) = r.handler.handle(ModelDriftOwnerRequest(dry_run=True)).lanes
    assert lane.dry_run
    assert lane.brief_checked
    assert not lane.started
    assert lane.brief_sha256 in r.briefs.stamped
    assert r.runner.placed == []


def test_a_dry_run_whose_brief_is_refused_says_so() -> None:
    r = rig((item(),))
    r.briefs.refuse = "REFUSED no section 3a"
    (lane,) = r.handler.handle(ModelDriftOwnerRequest(dry_run=True)).lanes
    assert lane.dry_run
    assert not lane.brief_checked
    assert "REFUSED no section 3a" in lane.detail


def test_the_runner_refuses_a_brief_the_check_never_stamped() -> None:
    """The control for the h053 reading: the runner side of the seam refuses the unstamped bytes, whatever the node says."""

    class _Liar:
        def __init__(self, inner: InMemoryBriefPreparer) -> None:
            self._inner = inner

        def prepare(self, *, lane: str, name: str, text: str) -> ModelBriefPrepared:
            self._inner.briefs[name] = text
            digest = hashlib.sha256(text.encode()).hexdigest()
            return ModelBriefPrepared(
                path=f"memory://{name}", passed=True, sha256=digest, detail=""
            )

    r = rig((item(),))
    handler = HandlerHostDriftOwnerDispatch(
        overlay=overlay(),
        items=r.items,
        live=r.live,
        record=r.record,
        briefs=_Liar(r.briefs),
        runner=r.runner,
        runner_path=RUNNER_PATH,
        claude_dir=CLAUDE_DIR,
        clock=lambda: NOW,
    )
    (lane,) = handler.handle(ModelDriftOwnerRequest(dry_run=False)).lanes
    assert not lane.started
    assert "REMOTE-LANE-BRIEF-UNCHECKED" in lane.detail
    assert r.record.record == {}


def test_the_stamped_bytes_are_the_bytes_at_the_path_the_runner_is_given() -> None:
    r = rig((item(),))
    (lane,) = r.handler.handle(ModelDriftOwnerRequest(dry_run=False)).lanes
    name = lane.brief_path.removeprefix("memory://")
    assert (
        hashlib.sha256(r.briefs.briefs[name].encode()).hexdigest() == lane.brief_sha256
    )
    assert lane.brief_sha256 in r.briefs.stamped
    (argv,) = r.runner.placed
    assert argv[argv.index("--brief") + 1] == lane.brief_path


def test_the_preparer_builds_the_brief_from_rules_ticket_text_and_constraints_then_checks_it(
    tmp_path: Path,
) -> None:
    prepared = _preparer(tmp_path).prepare(
        lane="host-drift-x-20261113T1000Z", name="x-brief.md", text=_task()
    )
    assert prepared.passed, prepared.detail
    text = Path(prepared.path).read_text(encoding="utf-8")
    assert text.index("Host drift: an open item") < text.index("## 3. Rules")
    assert "# Standing rules for every dispatched lane" in text
    assert "ticket-text: OMN-1 host drift" in text
    assert "## 3a. Constraints" in text
    assert hashlib.sha256(text.encode()).hexdigest() == prepared.sha256
    stamp = tmp_path / "stamps" / f"{prepared.sha256}.json"
    assert stamp.is_file()


def test_a_brief_without_the_overlays_lines_is_refused_by_the_check_with_its_reasons(
    tmp_path: Path,
) -> None:
    prepared = _preparer(tmp_path).prepare(
        lane="x", name="x-brief.md", text="Host drift: a task with no EXEMPLARS line\n"
    )
    assert not prepared.passed
    assert prepared.detail == "no EXEMPLARS line"
    assert not (tmp_path / "stamps").exists()


def test_a_real_pass_hands_the_runner_a_brief_it_accepts_and_an_unchecked_one_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _preparer(tmp_path).prepare(
        lane="host-drift-x-20261113T1000Z", name="x-brief.md", text=_task()
    )
    runner = tmp_path / "runner.py"
    runner.write_text(RUNNER)
    monkeypatch.setenv("STAMP_DIR", str(tmp_path / "stamps"))
    placer = SubprocessLanePlacer(30, tmp_path / "logs")
    accepted = placer.place(
        (sys.executable, str(runner), "--brief", prepared.path, "--lane", "x")
    )
    plain = tmp_path / "plain-brief.md"
    plain.write_text("a plain brief", encoding="utf-8")
    refused = placer.place(
        (sys.executable, str(runner), "--brief", str(plain), "--lane", "y")
    )
    assert accepted.started
    assert not refused.started
    assert "REMOTE-LANE-BRIEF-UNCHECKED" in refused.detail


def test_a_missing_ledger_path_is_a_brief_that_did_not_pass(tmp_path: Path) -> None:
    prepared = _preparer(tmp_path, ledger=None).prepare(
        lane="x", name="x-brief.md", text=_task()
    )
    assert not prepared.passed
    assert "EXAMPLE_LEDGER_PATH is not set" in prepared.detail
    assert prepared.sha256 == ""


class _Scripted:
    """A ``run`` that answers each lane-brief sub-command from its list, in order, and keeps the commands it was given."""

    def __init__(
        self, outcomes: dict[str, list[CompletedProcess[str] | Exception]]
    ) -> None:
        self.outcomes = outcomes
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_: object) -> CompletedProcess[str]:
        self.calls.append(argv)
        key = next(
            (w for w in ("ticket-text", "constraints", "check") if w in argv),
            "rules",
        )
        pending = self.outcomes[key]
        answer = pending.pop(0) if len(pending) > 1 else pending[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _scripted(
    outcomes: dict[str, list[CompletedProcess[str] | Exception]],
) -> _Scripted:
    return _Scripted(outcomes)


def _done(code: int, out: str = "", err: str = "") -> CompletedProcess[str]:
    return CompletedProcess([], code, out, err)


def test_a_check_that_timed_out_is_not_a_pass(tmp_path: Path) -> None:
    run = _scripted(
        {
            "rules": [_done(0, "rules")],
            "ticket-text": [_done(0, "ticket")],
            "constraints": [_done(0, "ROUTE: band=B3\n3a")],
            "check": [TimeoutExpired(["check"], 120)],
        }
    )
    prepared = _preparer(tmp_path, run=run).prepare(lane="x", name="b.md", text=_task())
    assert not prepared.passed
    assert "timed out" in prepared.detail


def test_a_check_with_no_verdict_is_named_as_such(tmp_path: Path) -> None:
    run = _scripted(
        {
            "rules": [_done(0, "rules")],
            "ticket-text": [_done(0, "ticket")],
            "constraints": [_done(0, "3a")],
            "check": [_done(3, "REFUSED h053: the check did not run on a lab host")],
        }
    )
    prepared = _preparer(tmp_path, run=run).prepare(lane="x", name="b.md", text=_task())
    assert not prepared.passed
    assert prepared.detail.startswith("no verdict on the brief:")


def test_a_ledger_row_between_the_3a_read_and_the_check_is_one_regeneration(
    tmp_path: Path,
) -> None:
    run = _scripted(
        {
            "rules": [_done(0, "rules")],
            "ticket-text": [_done(0, "ticket")],
            "constraints": [_done(0, "3a")],
            "check": [_done(3, "REFUSED regenerate 3a"), _done(0)],
        }
    )
    prepared = _preparer(tmp_path, run=run).prepare(lane="x", name="b.md", text=_task())
    assert prepared.passed
    assert sum("constraints" in call for call in run.calls) == 2


def test_the_check_command_carries_the_overlays_values(tmp_path: Path) -> None:
    run = _scripted(
        {
            "rules": [_done(0, "rules")],
            "ticket-text": [_done(0, "ticket")],
            "constraints": [_done(0, "3a")],
            "check": [_done(0)],
        }
    )
    _preparer(tmp_path, run=run).prepare(lane="x", name="b.md", text=_task())
    check = next(c for c in run.calls if "check" in c)
    assert check[check.index("--ledger") + 1] == LEDGER
    assert "--recompute" in check
    assert "--lab" in check
    assert check[check.index("--where") + 1] == "lab"
    constraints = next(c for c in run.calls if "constraints" in c)
    assert constraints[constraints.index("--score") + 1] == "9"
    assert constraints[constraints.index("--dims") + 1] == "S2A1R1V2N1C1J1"
    assert constraints[constraints.index("--model") + 1] == "sonnet"
    assert constraints[constraints.index("--ticket") + 1] == "OMN-1"
