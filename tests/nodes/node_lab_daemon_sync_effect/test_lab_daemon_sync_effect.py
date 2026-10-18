# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: a merged lab daemon converges to its source branch tip on the host tick.

Fixtures cover the three cases the operator kept meeting by hand: an installed revision that lags
main (installed through the daemon's own installer), a branch install whose expiry has passed
(reverted to main), and an installer that refuses (an alarm event naming the blocker, and one fix
lane dispatched through the remote-lane runner, not repeated on the next tick).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel

from omnimarket.models.model_lab_daemon_drift import ModelLabDaemonDrift
from omnimarket.nodes.node_lab_daemon_sync_effect.handlers.handler_lab_daemon_sync import (
    HandlerLabDaemonSync,
    decide_daemon,
)
from omnimarket.nodes.node_lab_daemon_sync_effect.models.model_lab_daemon_sync import (
    ModelDaemonFacts,
    ModelDaemonOverlay,
    ModelLabDaemonInstallRefused,
    ModelLabDaemonSyncCommand,
    ModelLabDaemonSyncResult,
)

pytestmark = pytest.mark.unit

NOW = "2026-10-17T03:00:00Z"
MAIN = "a" * 40
OLD = "b" * 40
REFUSED_TOPIC = "onex.evt.omnimarket.lab-daemon-install-refused.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.lab-daemon-sync-completed.v1"


class FakeHost:
    """The host boundary: probes, lock reads, installer and fix-lane runs, all recorded."""

    def __init__(
        self,
        installed: dict[str, str | None],
        source: dict[str, str],
        installer_result: dict[str, tuple[int, str, str]] | None = None,
        installs_to: dict[str, str] | None = None,
        busy: set[str] | None = None,
        records: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.installed = dict(installed)
        self.source = source
        self.installer_result = installer_result or {}
        self.installs_to = installs_to or {}
        self.busy = busy or set()
        self.records = records or {}
        self.installer_runs: list[list[str]] = []
        self.fix_lanes: list[list[str]] = []
        self.briefs: dict[str, str] = {}

    def run(self, argv: list[str], timeout_s: int) -> tuple[int, str, str]:
        if argv[0] == "probe-installed":
            value = self.installed.get(argv[1])
            return (0, f"{value}\n", "") if value else (1, "", "no installed record")
        if argv[0] == "probe-source":
            return 0, f"{self.source[argv[1]]}\n", ""
        if argv[0] == "install":
            self.installer_runs.append(argv)
            rc, out, err = self.installer_result.get(argv[1], (0, "", ""))
            if rc == 0 and argv[1] in self.installs_to:
                self.installed[argv[1]] = self.installs_to[argv[1]]
            return rc, out, err
        if argv[0] == "fix-lane":
            self.fix_lanes.append(argv)
            return 0, "detached\n", ""
        raise AssertionError(f"unexpected argv {argv}")

    def read_json(self, path: str) -> dict[str, object] | None:
        if path in self.records:
            return dict(self.records[path])
        file = Path(path)
        return json.loads(file.read_text()) if file.is_file() else None

    def lock_busy(self, path: str) -> bool:
        return path in self.busy

    def write_text(self, path: str, text: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text)
        if "/briefs/" in path:
            self.briefs[path] = text

    def exists(self, path: str) -> bool:
        return True


class FakePublisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, BaseModel]] = []

    def publish(self, topic: str, event: BaseModel) -> None:
        self.events.append((topic, event))

    def alarm(self, index: int = 0) -> ModelLabDaemonInstallRefused:
        event = self.events[index][1]
        assert isinstance(event, ModelLabDaemonInstallRefused)
        return event


def _daemon(name: str, **extra: object) -> dict[str, object]:
    return {
        "name": name,
        "installed": {"kind": "command", "argv": ["probe-installed", name]},
        "source": {"kind": "command", "argv": ["probe-source", name]},
        "installer": ["install", name],
        **extra,
    }


def _overlay(*daemons: dict[str, object]) -> ModelDaemonOverlay:
    return ModelDaemonOverlay.model_validate(
        {
            "schema_version": 1,
            "daemons": list(daemons),
            "fix_lane": ["fix-lane", "--lane", "{lane}", "--brief", "{brief}"],
        }
    )


def _run(
    host: FakeHost,
    overlay: ModelDaemonOverlay,
    tmp_path: Path,
    previous: dict[str, object] | None = None,
    now: str = NOW,
    mode: Literal["repair", "check"] = "repair",
) -> tuple[ModelLabDaemonSyncResult, FakePublisher]:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    if previous is not None:
        (state / "h200.json").write_text(json.dumps(previous))
    publisher = FakePublisher()
    handler = HandlerLabDaemonSync(
        host=host,
        publisher=publisher,
        refused_topic=REFUSED_TOPIC,
        completed_topic=COMPLETED_TOPIC,
    )
    result = handler.handle(
        ModelLabDaemonSyncCommand(
            host="h200",
            overlay=overlay,
            state_dir=str(state),
            now=now,
            mode=mode,
        )
    )
    return result, publisher


def test_lagging_daemon_installs_main_through_its_own_installer(
    tmp_path: Path,
) -> None:
    host = FakeHost(
        installed={"landing-controller": OLD},
        source={"landing-controller": MAIN},
        installs_to={"landing-controller": MAIN},
    )
    overlay = _overlay(_daemon("landing-controller", locks=["/s/install.lock"]))
    result, publisher = _run(host, overlay, tmp_path)

    assert host.installer_runs == [["install", "landing-controller"]]
    (outcome,) = result.daemons
    assert outcome.verdict == "installed"
    assert outcome.installed_revision == MAIN
    assert result.exit_code == 0
    assert result.drifts == []
    assert [topic for topic, _ in publisher.events] == [COMPLETED_TOPIC]
    written = json.loads((tmp_path / "state" / "h200.json").read_text())
    assert written["daemons"]["landing-controller"]["verdict"] == "installed"


def test_current_daemon_runs_no_installer(tmp_path: Path) -> None:
    host = FakeHost(installed={"pr-watcher": MAIN}, source={"pr-watcher": MAIN})
    result, _ = _run(host, _overlay(_daemon("pr-watcher")), tmp_path)
    assert host.installer_runs == []
    assert result.daemons[0].verdict == "current"


def test_branch_install_past_its_expiry_reverts_to_main(tmp_path: Path) -> None:
    record = "/install/landing_install.json"
    host = FakeHost(
        installed={"landing-controller": OLD},
        source={"landing-controller": MAIN},
        installs_to={"landing-controller": MAIN},
        records={
            record: {
                "branch_until": "2026-10-17T01:00:00Z",
                "branch_source": "/tmp/branch-skills",
            }
        },
    )
    overlay = _overlay(
        _daemon("landing-controller", branch_record={"path": record}),
    )
    result, _ = _run(host, overlay, tmp_path)
    (outcome,) = result.daemons
    assert outcome.decision == "branch_expired"
    assert outcome.verdict == "installed"
    assert host.installer_runs == [["install", "landing-controller"]]


def test_branch_install_before_its_expiry_is_held_and_not_installed(
    tmp_path: Path,
) -> None:
    record = "/install/landing_install.json"
    host = FakeHost(
        installed={"landing-controller": OLD},
        source={"landing-controller": MAIN},
        records={record: {"branch_until": "2026-10-17T05:00:00Z"}},
    )
    overlay = _overlay(_daemon("landing-controller", branch_record={"path": record}))
    result, _ = _run(host, overlay, tmp_path)
    (outcome,) = result.daemons
    assert outcome.verdict == "branch_held"
    assert host.installer_runs == []
    (drift,) = result.drifts
    assert drift.branch_until == "2026-10-17T05:00:00Z"


def test_installer_refusal_alarms_and_dispatches_one_fix_lane(
    tmp_path: Path,
) -> None:
    host = FakeHost(
        installed={"landing-controller": OLD},
        source={"landing-controller": MAIN},
        installer_result={
            "landing-controller": (
                2,
                "",
                "evidence error: [Errno 2] No such file: landing_install.json\n",
            )
        },
    )
    overlay = _overlay(_daemon("landing-controller"))
    result, publisher = _run(host, overlay, tmp_path)

    (outcome,) = result.daemons
    assert outcome.verdict == "refused"
    assert "landing_install.json" in (outcome.blocker or "")
    assert "rc=2" in (outcome.blocker or "")
    assert result.exit_code == 1
    topics = [topic for topic, _ in publisher.events]
    assert topics == [REFUSED_TOPIC, COMPLETED_TOPIC]
    alarm = publisher.alarm()
    assert alarm.daemon == "landing-controller"
    assert alarm.host == "h200"
    assert "landing_install.json" in alarm.blocker
    assert alarm.fix_lane_dispatched is True
    (lane,) = host.fix_lanes
    assert lane[:3] == ["fix-lane", "--lane", "daemon-sync-fix-landing-controller-h200"]
    (brief,) = host.briefs.values()
    assert "landing_install.json" in brief
    assert MAIN in brief
    (drift,) = result.drifts
    assert drift.lagging_since == NOW

    # The next tick meets the same blocker: it alarms again but dispatches no second lane.
    previous = json.loads((tmp_path / "state" / "h200.json").read_text())
    host.fix_lanes.clear()
    result2, publisher2 = _run(
        host, overlay, tmp_path, previous=previous, now="2026-10-17T03:05:00Z"
    )
    assert host.fix_lanes == []
    assert publisher2.alarm().fix_lane_dispatched is False
    assert result2.drifts[0].lagging_since == NOW


def test_installer_exit_zero_that_leaves_the_lag_is_a_refusal(tmp_path: Path) -> None:
    host = FakeHost(installed={"omni-plugin": OLD}, source={"omni-plugin": MAIN})
    result, publisher = _run(host, _overlay(_daemon("omni-plugin")), tmp_path)
    (outcome,) = result.daemons
    assert outcome.verdict == "refused"
    assert "still" in (outcome.blocker or "")
    assert publisher.events[0][0] == REFUSED_TOPIC


def test_held_locks_skip_the_tick_without_an_alarm(tmp_path: Path) -> None:
    host = FakeHost(
        installed={"landing-controller": OLD},
        source={"landing-controller": MAIN},
        busy={"/s/install.lock"},
    )
    overlay = _overlay(
        _daemon("landing-controller", locks=["/s/install.lock", "/s/tick.lock"])
    )
    result, publisher = _run(host, overlay, tmp_path)
    assert host.installer_runs == []
    assert result.daemons[0].verdict == "busy"
    assert [topic for topic, _ in publisher.events] == [COMPLETED_TOPIC]
    assert result.drifts[0].installed_revision == OLD


def test_unreadable_installed_revision_is_a_refusal_naming_the_probe(
    tmp_path: Path,
) -> None:
    host = FakeHost(installed={"pr-watcher": None}, source={"pr-watcher": MAIN})
    result, publisher = _run(host, _overlay(_daemon("pr-watcher")), tmp_path)
    (outcome,) = result.daemons
    assert outcome.verdict == "refused"
    assert "installed revision unreadable" in (outcome.blocker or "")
    assert host.installer_runs == []
    assert len(host.fix_lanes) == 1
    assert publisher.events[0][0] == REFUSED_TOPIC


def test_check_mode_installs_nothing_and_dispatches_nothing(tmp_path: Path) -> None:
    host = FakeHost(installed={"pr-watcher": OLD}, source={"pr-watcher": MAIN})
    result, _ = _run(host, _overlay(_daemon("pr-watcher")), tmp_path, mode="check")
    assert host.installer_runs == []
    assert host.fix_lanes == []
    assert result.daemons[0].verdict == "lagging"


def test_daemon_absent_from_this_host_is_skipped(tmp_path: Path) -> None:
    class Absent(FakeHost):
        def exists(self, path: str) -> bool:
            return False

    host = Absent(installed={}, source={})
    overlay = _overlay(_daemon("landing-controller", present_when="/plist"))
    result, _ = _run(host, overlay, tmp_path)
    assert result.daemons == []


@pytest.mark.parametrize(
    ("installed", "source", "branch_until", "expected"),
    [
        (MAIN, MAIN, None, "current"),
        (OLD, MAIN, None, "lagging"),
        (OLD, MAIN, "2026-10-17T02:59:59Z", "branch_expired"),
        (OLD, MAIN, "2026-10-17T03:00:01Z", "branch_held"),
        (OLD, MAIN, "not-a-stamp", "unreadable"),
        (None, MAIN, None, "unreadable"),
    ],
)
def test_decide_daemon(
    installed: str | None, source: str, branch_until: str | None, expected: str
) -> None:
    facts = ModelDaemonFacts(
        installed_revision=installed,
        installed_error=None if installed else "no record",
        source_revision=source,
        branch_until=branch_until,
    )
    assert decide_daemon(facts, NOW) == expected


def test_drift_model_round_trips_the_state_file_shape() -> None:
    drift = ModelLabDaemonDrift(
        daemon="landing-controller",
        host="h200",
        installed_revision=OLD,
        source_revision=MAIN,
        lagging_since=NOW,
    )
    assert ModelLabDaemonDrift.model_validate_json(drift.model_dump_json()) == drift


def test_shipped_overlay_validates() -> None:
    import yaml

    path = (
        Path(__file__).parents[3]
        / "src/omnimarket/nodes/node_lab_daemon_sync_effect/overlays/lab_daemons.yaml"
    )
    overlay = ModelDaemonOverlay.model_validate(yaml.safe_load(path.read_text()))
    names = {daemon.name for daemon in overlay.daemons}
    assert {"landing-controller", "pr-watcher", "omni-plugin", "dispatch-venv"} <= names


def test_overlay_expands_set_variables_and_skips_a_daemon_naming_an_unset_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_lab_daemon_sync_effect.__main__ import load_overlay

    path = tmp_path / "overlay.yaml"
    path.write_text(
        """\
schema_version: 1
fix_lane: ["echo", "${LDS_SET}"]
daemons:
  - name: alpha
    installer: ["${LDS_SET}/install"]
    installed:
      kind: command
      argv: ["git", "-C", "$LDS_SET", "rev-parse", "HEAD"]
    source:
      kind: command
      argv: ["git", "-C", "$LDS_SET", "rev-parse", "HEAD"]
  - name: beta
    installer: ["${LDS_UNSET}/install"]
    installed:
      kind: command
      argv: ["git", "rev-parse", "HEAD"]
    source:
      kind: command
      argv: ["git", "rev-parse", "HEAD"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("LDS_SET", "/x")
    monkeypatch.delenv("LDS_UNSET", raising=False)
    diagnostics: list[str] = []

    overlay = load_overlay(path, diagnostics)

    assert [daemon.name for daemon in overlay.daemons] == ["alpha"]
    assert overlay.daemons[0].installer == ["/x/install"]
    assert "/x" in overlay.daemons[0].source.argv
    assert overlay.fix_lane == ["echo", "/x"]
    assert diagnostics == ["beta: skipped, unset LDS_UNSET"]


def test_overlay_refuses_a_fix_lane_naming_an_unset_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_lab_daemon_sync_effect.__main__ import load_overlay

    path = tmp_path / "overlay.yaml"
    path.write_text(
        """\
schema_version: 1
fix_lane: ["echo", "${LDS_UNSET}"]
daemons: []
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("LDS_UNSET", raising=False)

    with pytest.raises(ValueError, match="fix_lane names unset LDS_UNSET"):
        load_overlay(path, [])


def test_models_refuse_empty_argv_elements_and_impossible_stamps() -> None:
    from pydantic import ValidationError

    from omnimarket.nodes.node_lab_daemon_sync_effect.models.model_lab_daemon_sync import (
        ModelRevisionProbe,
    )

    with pytest.raises(ValidationError):
        ModelRevisionProbe(kind="command", argv=["git", ""])
    with pytest.raises(ValidationError):
        ModelRevisionProbe(kind="command")
    with pytest.raises(ValidationError):
        ModelRevisionProbe(kind="json_file")

    overlay = ModelDaemonOverlay.model_validate(
        {
            "schema_version": 1,
            "daemons": [_daemon("alpha")],
            "fix_lane": ["echo"],
        }
    )
    with pytest.raises(ValidationError):
        ModelLabDaemonSyncCommand(
            host="h200",
            overlay=overlay,
            state_dir="/state",
            now="2026-13-45T99:99:99Z",
        )
