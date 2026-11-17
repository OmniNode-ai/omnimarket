# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The checks the OMN-24874 contract binds are real readings, not greps that also match a failure (OMN-24874 contract)."""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.handlers.delegation_scenario_runtime import JournalScenarioEventSink
from omnimarket.models.model_delegation_scenario import EnumScenarioTrigger
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.adapter_r8_lane import (
    InMemoryLane,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.handler_scenario_r8 import (
    HandlerScenarioR8,
    load_runtime,
)

_CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "OMN-24874.yaml"
# The proxy items that bound AC2, AC3 and AC5 to tests that pass whether or not the AC holds.
_REMOVED_PROXIES = {
    "dod-omn24874-ac2-r9-node",
    "dod-omn24874-ac3-fix-in-tree",
    "dod-omn24874-ac5-reviewed-head",
}


def _items() -> dict[str, dict[str, Any]]:
    document = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return {item["id"]: item for item in document["dod_evidence"]}


def _commands() -> list[str]:
    return [
        str(check["check_value"])
        for item in _items().values()
        for check in item["checks"]
    ]


@pytest.mark.unit
def test_omn24874_contract_has_no_grep_for_success() -> None:
    # `grep -q success` also matches {"success": false} and {"conclusion": "failure", "note": "no success"}.
    assert not [c for c in _commands() if "grep -q success" in c]


_PINNED_HOST = "hp-gen9"  # h053's hostname, the host every live reading of the contract is pinned to


def _hostname_shim(directory: Path, name: str) -> dict[str, str]:
    """An environment whose ``hostname`` prints ``name``, so a host pin can be shown to hold and to refuse."""
    shim = directory / "bin" / "hostname"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text(f"#!/bin/sh\necho {name}\n", encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return {"PATH": f"{shim.parent}:/usr/bin:/bin:/usr/local/bin"}


_UNITS_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_host_installer_registry_units/contract.yaml"
)
_SCENARIO_UNITS = {"R8": "scenario-r8-systemd", "R9": "scenario-r9-systemd"}


def _max_age(scenario: str) -> int:
    """The age past which a scenario's last-run record reads stale: the unit's declared start_interval plus a quarter.

    Read from the registry units contract the units are installed from, so the live commands' numbers follow the unit's
    schedule and are not pinned by this file.
    """
    units = yaml.safe_load(_UNITS_CONTRACT.read_text(encoding="utf-8"))["units"]
    interval = int(units[_SCENARIO_UNITS[scenario]]["schedule"]["start_interval"])
    return interval + interval // 4


@pytest.mark.unit
def test_omn24874_contract_live_reading_parses_the_conclusion_field(
    tmp_path: Path,
) -> None:
    command = str(_items()["dod-omn24874-live-readings"]["checks"][0]["check_value"])

    def run(
        r8: Mapping[str, object], r9: Mapping[str, object], host: str = _PINNED_HOST
    ) -> int:
        for name, record in (("r8", r8), ("r9", r9)):
            path = (
                tmp_path / f".local/state/omni/delegation-scenario-{name}/last-run.json"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record), encoding="utf-8")
        return subprocess.run(
            shlex.split(command),
            env={"HOME": str(tmp_path), **_hostname_shim(tmp_path, host)},
            check=False,
            capture_output=True,
        ).returncode

    fresh = datetime.now(UTC).isoformat()
    ok = {"conclusion": "success", "ran_at": fresh}
    assert run(ok, ok) == 0
    # The shapes a grep for the word success accepts, and a parse of the conclusion rejects.
    assert run({"conclusion": "failure", "success": True, "ran_at": fresh}, ok) != 0
    assert (
        run(
            ok,
            {"conclusion": "refused", "refused": "no success route", "ran_at": fresh},
        )
        != 0
    )
    assert run(ok, {"success": False, "conclusion": "failure", "ran_at": fresh}) != 0
    # Host and age are pinned: another host, or a record older than the alarm contract's max age, fails.
    assert run(ok, ok, host="some-other-host") != 0
    stale_r8 = (datetime.now(UTC) - timedelta(seconds=_max_age("R8") + 60)).isoformat()
    stale_r9 = (datetime.now(UTC) - timedelta(seconds=_max_age("R9") + 60)).isoformat()
    assert run({"conclusion": "success", "ran_at": stale_r8}, ok) != 0
    assert run(ok, {"conclusion": "success", "ran_at": stale_r9}) != 0
    # Inside the age it passes: the pin is the contract's own number, not a number that always fails.
    inside = (datetime.now(UTC) - timedelta(seconds=_max_age("R9") - 600)).isoformat()
    assert run(ok, {"conclusion": "success", "ran_at": inside}) == 0


@pytest.mark.unit
def test_omn24874_contract_live_command_ages_follow_the_units_declared_intervals() -> (
    None
):
    # A number in a command that drifts from the unit's interval plus a quarter is a live reading with the wrong tolerance.
    items = _items()
    live = str(items["dod-omn24874-live-readings"]["checks"][0]["check_value"])
    assert f'"r8 {_max_age("R8")}"' in live
    assert f'"r9 {_max_age("R9")}"' in live
    r9 = str(items["dod-omn24874-ac2-r9-live"]["checks"][0]["check_value"])
    assert r9.rstrip("' ").endswith(f"ran_at {_max_age('R9')}")
    r8 = str(items["dod-omn24874-ac1-r8-live-13-of-13"]["checks"][0]["check_value"])
    assert r8.rstrip("' ").endswith(f"recorded_at {_max_age('R8')}")
    for command in (live, r9, r8):
        assert f'test "$(hostname -s)" = {_PINNED_HOST}' in command


def _write_real_r8_row(home: Path, *, run_cost_usd: Decimal) -> None:
    """One R8 run through the real R8 handler, its result journaled by the real journal sink (the writer of h053's events.jsonl)."""
    journal = home / ".onex_state/delegation-scenario-r8/events.jsonl"
    handler = HandlerScenarioR8(
        load_runtime(),
        home=home,
        environment={},
        appender=lambda _row: None,
        sink=JournalScenarioEventSink(journal),
        terminal_topic="onex.evt.omnimarket.delegation-scenario-result.v1",
        lane_factory=lambda: InMemoryLane(run_cost_usd=run_cost_usd),
        trigger=EnumScenarioTrigger.SCHEDULE,
        clock=lambda: datetime.now(UTC),
        sleep=lambda _seconds: None,
    )
    handler.run()


@pytest.mark.unit
def test_omn24874_contract_r9_live_command_reads_the_conclusion_host_and_age(
    tmp_path: Path,
) -> None:
    # The command of dod-omn24874-ac2-r9-live itself (not the combined readings item), run over a real last-run.json.
    command = str(_items()["dod-omn24874-ac2-r9-live"]["checks"][0]["check_value"])
    record_path = tmp_path / ".local/state/omni/delegation-scenario-r9/last-run.json"
    record_path.parent.mkdir(parents=True, exist_ok=True)

    def run(record: Mapping[str, object] | str, host: str = _PINNED_HOST) -> int:
        record_path.write_text(
            record if isinstance(record, str) else json.dumps(record), encoding="utf-8"
        )
        return subprocess.run(
            shlex.split(command),
            env={"HOME": str(tmp_path), **_hostname_shim(tmp_path, host)},
            check=False,
            capture_output=True,
        ).returncode

    now = datetime.now(UTC)
    fresh = now.isoformat()
    inside = (now - timedelta(seconds=_max_age("R9") - 600)).isoformat()
    stale = (now - timedelta(seconds=_max_age("R9") + 60)).isoformat()
    # success inside the age passes; a record read on the pinned host just inside the bound is the boundary control.
    assert run({"conclusion": "success", "ran_at": fresh}) == 0
    assert run({"conclusion": "success", "ran_at": inside}) == 0
    # failure and refused fail, including the shapes a grep for the word success accepts.
    assert run({"conclusion": "failure", "ran_at": fresh}) != 0
    assert run({"conclusion": "failure", "success": True, "ran_at": fresh}) != 0
    assert (
        run({"conclusion": "refused", "refused": "no success route", "ran_at": fresh})
        != 0
    )
    assert run({"success": False, "conclusion": "failure", "ran_at": fresh}) != 0
    # a success record older than the alarm contract's R9 max age is stale.
    assert run({"conclusion": "success", "ran_at": stale}) != 0
    # another host fails even with a fresh success record.
    assert run({"conclusion": "success", "ran_at": fresh}, host="some-other-host") != 0
    # an unreadable record and a record with no time fail.
    assert run("not json") != 0
    assert run({"conclusion": "success"}) != 0


@pytest.mark.unit
def test_omn24874_contract_r8_reading_means_cost_above_zero_on_a_real_writer_row(
    tmp_path: Path,
) -> None:
    # +organic_run_has_values is ``tokens > 0 and cost_usd > 0`` (handler_scenario_r8.py): the 13 of 13 reading rests on it.
    command = str(
        _items()["dod-omn24874-ac1-r8-live-13-of-13"]["checks"][0]["check_value"]
    )
    env = {"OMNI_HOME": str(tmp_path), **_hostname_shim(tmp_path, _PINNED_HOST)}

    def reading() -> int:
        return subprocess.run(
            shlex.split(command), env=env, check=False, capture_output=True
        ).returncode

    _write_real_r8_row(tmp_path, run_cost_usd=Decimal("0.000145"))
    journal = tmp_path / ".onex_state/delegation-scenario-r8/events.jsonl"
    row = json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])["payload"]
    assert row["conclusion"] == "success"
    assert len(row["assertions"]) == 13
    assert "+organic_run_has_values" in row["assertions"]
    assert reading() == 0
    # The live h053 row of 2026-11-16T18:06Z: the organic control cost 0, so the one assertion read red and the reading fails.
    journal.unlink()
    _write_real_r8_row(tmp_path, run_cost_usd=Decimal(0))
    zero = json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])["payload"]
    assert zero["conclusion"] == "failure"
    assert "-organic_run_has_values" in zero["assertions"]
    assert reading() != 0


_ALARM_HEAD = "d65bc516360e8a0324f044cb0d637c9309665874"  # the head the watcher state records for the alarm pull request


def _plugin_with_ledger_rows(
    tmp_path: Path, rows: list[str], *, head: str, alarm_pr_state: str | None
) -> Path:
    """A plugin root whose lane_brief.py prints ``rows`` (``<HEAD>`` and ``<ALARM>`` filled in) and whose pr_state_local.py
    prints the watcher record of the alarm pull request (``alarm_pr_state`` None: the watcher cannot read it, exit 3)."""
    plugin = tmp_path / "plugin"
    script = plugin / "skills/lane-brief/scripts/lane_brief.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "import sys\nfor row in "
        + repr(rows)
        + ":\n    print(row.replace('<HEAD>', "
        + repr(head)
        + ").replace('<ALARM>', "
        + repr(_ALARM_HEAD)
        + "))\n",
        encoding="utf-8",
    )
    state = plugin / "skills/merge-drain/scripts/pr_state_local.py"
    state.parent.mkdir(parents=True)
    if alarm_pr_state is None:
        state.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    else:
        state.write_text(
            "import sys\n"
            "assert sys.argv[1:] == ['--pr', 'OmniNode-ai/omnimarket#6353', '--json'], sys.argv\n"
            f"print({json.dumps(json.dumps({'facts': {'head_sha': alarm_pr_state}}))})\n",
            encoding="utf-8",
        )
    return plugin


def _checkout_head(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "head")
    return repo, _git(repo, "rev-parse", "HEAD")


def _run_ac5(
    tmp_path: Path, rows: list[str], *, alarm_pr_state: str | None = _ALARM_HEAD
) -> int:
    repo, head = _checkout_head(tmp_path)
    plugin = _plugin_with_ledger_rows(
        tmp_path, rows, head=head, alarm_pr_state=alarm_pr_state
    )
    command = str(
        _items()["dod-omn24874-ac5-clean-verdict-row"]["checks"][0]["check_value"]
    )
    env = {
        "OMNI_PLUGIN_ROOT": str(plugin),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(tmp_path),
    }
    return subprocess.run(
        shlex.split(command), cwd=repo, env=env, check=False, capture_output=True
    ).returncode


_CLEAN = "2026-11-16T21:00:00Z | STATUS | lane=x | pr=OmniNode-ai/omnimarket#{pr} | head={head} | verdict=CLEAN"


@pytest.mark.unit
def test_omn24874_contract_ac5_check_needs_a_clean_row_at_the_head_of_each_pull_request(
    tmp_path: Path,
) -> None:
    older = "2026-11-16T19:44:33Z | STATUS | lane=x | pr=omnimarket#6335 | head=32e9b16c909d | verdict=CLEAN"
    this = _CLEAN.format(pr=6335, head="<HEAD>")
    alarm = _CLEAN.format(pr=6353, head="<ALARM>")
    # Both pull requests have a CLEAN row naming their current head: passes.
    assert _run_ac5(tmp_path / "both", [older, this, alarm]) == 0
    # The row of a head this checkout is not at (an older CLEAN row after a new push) passes nothing.
    assert _run_ac5(tmp_path / "stale", [older, alarm]) != 0
    # A CLEAN row for #6335 alone does not pass: the alarm pull request has none (the Done gate read passed with #6353 unmerged).
    assert _run_ac5(tmp_path / "no-alarm-row", [older, this]) != 0
    # A CLEAN row for an older head of #6353 is not its head.
    old_alarm = _CLEAN.format(pr=6353, head="0123456789ab")
    assert _run_ac5(tmp_path / "old-alarm-head", [this, old_alarm]) != 0
    # The row that names the other pull request's head does not stand in for #6353's own.
    wrong = _CLEAN.format(pr=6353, head="<HEAD>")
    assert _run_ac5(tmp_path / "wrong-head", [this, wrong]) != 0
    # A head the watcher cannot read fails closed.
    assert _run_ac5(tmp_path / "unreadable", [this, alarm], alarm_pr_state=None) != 0


_RUNTIME_CONTAINER = "omninode-runtime"
_RUNTIME_PROJECT = (
    "omnibase-infra"  # the compose project the dev runtime container carries as a label
)


def _git(repo: Path, *args: str, env: Mapping[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
        env={**scrub_git_location_env(os.environ), **(env or {})},
    ).stdout.strip()


class _Ac3World:
    """A temp omnimarket clone (base, the squash commit of the fix, a later dev commit tagged dev-*), the R8 unit's
    installed.json, and ssh and hostname shims standing in for the dev runtime host and its container."""

    def __init__(self, root: Path, *, tag_age_days: int = 0) -> None:
        self.root = root
        self.repo = root / "omnimarket"
        self.repo.mkdir(parents=True)
        _git(self.repo, "init", "-q")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "base")
        self.base = _git(self.repo, "rev-parse", "HEAD")
        _git(
            self.repo,
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "OMN-24874: the fix (#6335)",
        )
        self.fix = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "a later change")
        self.later = _git(self.repo, "rev-parse", "HEAD")
        self.tag = f"dev-20261117000000-{self.later[:12]}"
        if tag_age_days:
            # An annotated tag carries its own creation date: the newest dev-* tag is then older than a day.
            then = (datetime.now(UTC) - timedelta(days=tag_age_days)).isoformat()
            _git(
                self.repo,
                "tag",
                "-a",
                "-m",
                "t",
                self.tag,
                self.later,
                env={"GIT_COMMITTER_DATE": then},
            )
        else:
            _git(self.repo, "tag", self.tag, self.later)
        _git(self.repo, "update-ref", "refs/remotes/origin/dev", self.later)
        self.installed = root / ".onex_state/delegation-scenario-r8"
        self.installed.mkdir(parents=True)
        self.set_installed(self.later)
        self.bin = root / "bin"
        self.bin.mkdir()
        self.calls = root / "ssh-calls.txt"
        self.set_runtime()

    def set_installed(self, revision: str) -> None:
        (self.installed / "scenario-r8-systemd.installed.json").write_text(
            json.dumps({"revision": revision}), encoding="utf-8"
        )

    def set_runtime(
        self,
        *,
        container: str = _RUNTIME_CONTAINER,
        project: str = _RUNTIME_PROJECT,
        commit: str | None = None,
        dev_merge_tag: str | None = None,
        dirty: bool = False,
        sibling: str = "omnimarket",
        symbols_present: bool = True,
    ) -> None:
        # The top-level keys the real /app/build-provenance.json requires (build-provenance-schema.json in omnibase_infra).
        provenance = {
            "build_source": "workspace",
            "build_time": "2026-11-17T00:00:00Z",
            "infra_vcs_ref": self.later,
            "per_repo_vcs_provenance": {
                "siblings": {
                    "omnibase_core": {
                        "vcs_ref": self.later,
                        "vcs_dirty": False,
                        "dev_merge_tag": "dev-20261115210713-67a6d77ec939",
                        "vcs_branch": "HEAD",
                    },
                    sibling: {
                        "vcs_ref": commit if commit is not None else self.later,
                        "vcs_dirty": dirty,
                        "dev_merge_tag": (
                            self.tag if dev_merge_tag is None else dev_merge_tag
                        ),
                        # The record carries the sibling's branch too (build-provenance-schema.json requires it).
                        "vcs_branch": "HEAD",
                    },
                }
            },
        }
        inspect = json.dumps(
            [
                {
                    "Name": f"/{container}",
                    "Config": {"Labels": {"com.docker.compose.project": project}},
                }
            ]
        )
        (self.root / "provenance.json").write_text(
            json.dumps(provenance), encoding="utf-8"
        )
        shim = self.bin / "ssh"
        shim.write_text(
            "#!/bin/sh\n"
            f'echo "$@" >> {shlex.quote(str(self.calls))}\n'
            f'case "$*" in\n'
            f"  *'cat /app/build-provenance.json'*)\n"
            f"    case \"$*\" in *'docker inspect {_RUNTIME_CONTAINER}'*'docker exec {_RUNTIME_CONTAINER} '*) ;; *) exit 1;; esac\n"
            f"    echo {shlex.quote(inspect)}; cat {shlex.quote(str(self.root / 'provenance.json'))};;\n"
            f"  *'docker exec {_RUNTIME_CONTAINER} python -c'*) exit {0 if symbols_present else 1};;\n"
            f"  *) exit 1;;\n"
            f"esac\n",
            encoding="utf-8",
        )
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)

    def run(
        self,
        command: str,
        *,
        gate_host: str = _PINNED_HOST,
        ssh_host: str | None = "dev@runtime",
    ) -> int:
        hostname = _hostname_shim(self.root, gate_host)
        env = {
            "PATH": f"{self.bin}:{hostname['PATH']}",
            "OMNI_HOME": str(self.root),
            "HOME": str(self.root),
            # The operator variable the previous reading took its container from; the pinned reading must not follow it, so it
            # names a container that is not the pinned one (the shim answers the pinned name only).
            "RUNTIME_CONTAINER": "some-other-container",
        }
        if ssh_host is not None:
            env["ONEX_RUNTIME_SSH_HOST"] = ssh_host
        return subprocess.run(
            shlex.split(command),
            cwd=self.repo,
            env=env,
            check=False,
            capture_output=True,
        ).returncode


def _ac3_command() -> str:
    item = _items()["dod-omn24874-ac3-running-revision"]
    assert item["execution_scope"] == "local_done_gate"
    return str(item["checks"][0]["check_value"])


@pytest.mark.unit
def test_omn24874_contract_ac3_check_passes_when_host_container_revision_and_symbols_hold(
    tmp_path: Path,
) -> None:
    world = _Ac3World(tmp_path)
    assert world.run(_ac3_command()) == 0
    # The reading went to the pinned container, not to whichever container the operator variables name.
    calls = world.calls.read_text(encoding="utf-8")
    assert f"docker exec {_RUNTIME_CONTAINER} cat /app/build-provenance.json" in calls
    assert f"docker exec {_RUNTIME_CONTAINER} python -c" in calls


@pytest.mark.unit
def test_omn24874_contract_ac3_check_fails_when_the_running_revision_lacks_the_fix(
    tmp_path: Path,
) -> None:
    # The container holds the code of an older dev-merge tag: the fix is not its ancestor, so any container that merely
    # imports the symbol (the previous reading) is not enough.
    world = _Ac3World(tmp_path)
    world.set_runtime(commit=world.base)
    assert world.run(_ac3_command()) != 0


@pytest.mark.unit
@pytest.mark.parametrize(
    "runtime",
    [
        {"project": "omnibase-infra-stability"},
        {"container": "omninode-runtime-lane"},
        {"dev_merge_tag": ""},
        {"dirty": True},
        {"sibling": "omnibase_infra"},
        {"symbols_present": False},
    ],
)
def test_omn24874_contract_ac3_check_fails_on_the_wrong_container_build_record_or_missing_symbol(
    tmp_path: Path, runtime: dict[str, object]
) -> None:
    world = _Ac3World(tmp_path)
    world.set_runtime(**cast(dict[str, Any], runtime))
    assert world.run(_ac3_command()) != 0


@pytest.mark.unit
def test_omn24874_contract_ac3_check_fails_on_the_unpinned_inputs(
    tmp_path: Path,
) -> None:
    command = _ac3_command()
    world = _Ac3World(tmp_path)
    assert world.run(command, ssh_host=None) != 0  # no runtime host named
    assert world.run(command, gate_host="some-other-host") != 0
    # The R8 unit installed at a revision before the fix.
    world.set_installed(world.base)
    assert world.run(command) != 0


@pytest.mark.unit
def test_omn24874_contract_ac3_check_fails_before_the_fix_is_merged(
    tmp_path: Path,
) -> None:
    world = _Ac3World(tmp_path)
    _git(world.repo, "update-ref", "refs/remotes/origin/dev", world.base)
    assert world.run(_ac3_command()) != 0


@pytest.mark.unit
def test_omn24874_contract_ac3_check_needs_the_squash_commit_not_a_ticket_prefixed_subject(
    tmp_path: Path,
) -> None:
    # Only the commit whose subject ends (#6335) is the fix: a newer commit that merely starts with the ticket id is not.
    world = _Ac3World(tmp_path)
    _git(world.repo, "commit", "-q", "--allow-empty", "-m", "OMN-24874: a follow-up")
    follow_up = _git(world.repo, "rev-parse", "HEAD")
    _git(world.repo, "tag", f"dev-20261117010000-{follow_up[:12]}", follow_up)
    _git(world.repo, "update-ref", "refs/remotes/origin/dev", follow_up)
    world.set_installed(world.later)
    world.set_runtime(commit=world.later)
    # The newest commit named for the ticket is not an ancestor of the running revision; the squash commit is, so it passes.
    assert world.run(_ac3_command()) == 0


@pytest.mark.unit
def test_omn24874_contract_ac3_check_fails_when_the_dev_merge_tag_names_another_commit(
    tmp_path: Path,
) -> None:
    # A tag that merely starts with dev- but names a commit other than the build's vcs_ref passes nothing.
    world = _Ac3World(tmp_path)
    world.set_runtime(dev_merge_tag="dev-20261116081630-6efbe3ee5027")
    assert world.run(_ac3_command()) != 0
    world.set_runtime(dev_merge_tag=f"dev-20261117000000-{world.base[:12]}")
    assert world.run(_ac3_command()) != 0


@pytest.mark.unit
def test_omn24874_contract_ac3_check_fails_when_no_dev_merge_tag_contains_the_installed_revision(
    tmp_path: Path,
) -> None:
    world = _Ac3World(tmp_path)
    assert world.run(_ac3_command()) == 0
    # The R8 unit is installed at a commit past the newest dev-merge tag: no tag contains it.
    _git(world.repo, "commit", "-q", "--allow-empty", "-m", "an untagged change")
    world.set_installed(_git(world.repo, "rev-parse", "HEAD"))
    assert world.run(_ac3_command()) != 0


@pytest.mark.unit
def test_omn24874_contract_ac3_check_fails_when_the_newest_dev_merge_tag_is_over_a_day_old(
    tmp_path: Path,
) -> None:
    assert _Ac3World(tmp_path / "fresh").run(_ac3_command()) == 0
    assert _Ac3World(tmp_path / "old", tag_age_days=3).run(_ac3_command()) != 0


@pytest.mark.unit
def test_omn24874_contract_ac3_dev_runtime_symbols_exist_only_with_the_fix() -> None:
    # The two names the dev-runtime reading asserts: the workflow's declared-price function and the projection's overlay floor.
    from omnimarket.nodes.node_delegation_orchestrator.handlers import (
        handler_delegation_workflow as workflow,
    )
    from omnimarket.nodes.node_projection_delegation.handlers import (
        handler_projection_delegation as projection,
    )

    assert callable(workflow.byok_declared_price_per_1m)
    assert callable(projection._tenant_overlay_cost_usd)
    command = _ac3_command()
    assert "w.byok_declared_price_per_1m" in command
    assert "p._tenant_overlay_cost_usd" in command


@pytest.mark.unit
def test_omn24874_contract_binds_no_proxy_test_to_ac2_ac3_or_ac5() -> None:
    # AC2 (the R9 record), AC3 (the running revision) and AC5 (the CLEAN rows) are facts about a host, a deployment and the
    # ledger. A hosted test cannot go red with R9 red or the fix undeployed, so a bound test would be a proxy that passes
    # whether or not the AC holds. The hosted gate also refuses a bound local_done_gate item (see the repo-evidence binds
    # test), so these three readings stay unbound local_done_gate items and the contract declares AC1 only.
    document = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    declared = {
        ac["id"]
        for requirement in document["requirements"]
        for ac in requirement["acceptance"]
    }
    assert declared == {"AC1"}
    items = _items()
    assert not (_REMOVED_PROXIES & set(items))
    for item in items.values():
        assert not (set(item.get("binds_ac") or []) & {"AC2", "AC3", "AC5"}), item["id"]
    for live in (
        "dod-omn24874-ac2-r9-live",
        "dod-omn24874-ac3-running-revision",
        "dod-omn24874-ac5-clean-verdict-row",
    ):
        assert items[live]["execution_scope"] == "local_done_gate", live
        assert not items[live].get("binds_ac"), live
    # The hosted behavioural tests of the reading commands run as the whole file: a -k selection that a rename could empty
    # would shrink the evidence without a failure.
    runs = [
        c["check_value"]
        for item in items.values()
        for c in item["checks"]
        if "tests/test_omn24874_contract_checks.py" in c["check_value"]
    ]
    assert runs, "no dod item runs this file"
    assert all(" -k " not in run for run in runs), runs


@pytest.mark.unit
def test_omn24874_contract_text_names_no_rule_the_code_no_longer_has() -> None:
    # The ceiling readers skip the overlay tier because resolve_tier_cost returns None for it; there is no per-reader rule.
    text = _CONTRACT.read_text(encoding="utf-8")
    assert "by their own rule" not in text
    assert "resolve_tier_cost returns None for the tenant_overlay tier" in text
    assert "AC4" not in text, "the alarm criterion is omnimarket#6353's contract"


_CONTRACTS = _CONTRACT.parent
_READERS_TEST = (
    Path(__file__).resolve().parent
    / "unit"
    / "delegation"
    / "test_omn24874_tenant_overlay_budget_readers.py"
)
# Texts that name a per-reader tier rule or a line of handler_delegation.py: the ceiling readers skip the overlay tier because
# resolve_tier_cost returns None for it, and the guard that used to sit in handler_delegation.py is gone.
_STALE_TIER_RULE_TEXT = (
    "by their own rule",
    "tier rule at ``handler_delegation``",
    "tier rule at handler_delegation",
    "lines 1657",
    "1657 and 1658",
)


@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    [
        _CONTRACTS / "OMN-24874.yaml",
        _CONTRACTS / "OMN-25168.yaml",
        _CONTRACTS / "OMN-25186.yaml",
        _CONTRACTS / "OMN-25306.yaml",
        _READERS_TEST,
    ],
    ids=lambda p: p.name,
)
def test_omn24874_contract_stale_tier_rule_text_is_gone_from_all_three_files(
    path: Path,
) -> None:
    text = path.read_text(encoding="utf-8")
    assert text, path
    found = [stale for stale in _STALE_TIER_RULE_TEXT if stale in text]
    assert not found, (path.name, found)


@pytest.mark.unit
def test_omn24874_contract_stale_tier_rule_text_check_sees_the_old_text() -> None:
    # Positive control: the phrases are the ones the files carried at the reviewed head.
    old = "ceiling budget readers skip the tenant_overlay tier by their own rule"
    assert any(stale in old for stale in _STALE_TIER_RULE_TEXT)


@pytest.mark.unit
def test_omn24874_contract_alarm_reading_is_owed_to_a_lane_on_6353_in_both_contracts() -> (
    None
):
    # omnimarket#6353 carries the alarm node and its tests, not the AC4 reading of the alarm unit's install record and a newer
    # row (read at its head). Both contracts say the reading is owed to a lane on that pull request, in the same words.
    owed = "owed to a lane on omnimarket#6353"
    for ticket in ("OMN-25186", "OMN-25306"):
        text = (_CONTRACTS / f"{ticket}.yaml").read_text(encoding="utf-8")
        assert owed in text, ticket
    assert "does not carry that reading or those tests at its read head" not in (
        _CONTRACTS / "OMN-25186.yaml"
    ).read_text(encoding="utf-8")


@pytest.mark.unit
def test_omn24874_contract_stale_text_test_is_bound_in_the_omn25306_ac3_item() -> None:
    document = yaml.safe_load(
        (_CONTRACTS / "OMN-25306.yaml").read_text(encoding="utf-8")
    )
    item = next(
        i
        for i in document["dod_evidence"]
        if i["id"] == "dod-omn25306-ac3-cost-logic-callers"
    )
    assert item["binds_ac"] == ["AC3"]
    assert any(
        "tests/test_omn24874_contract_checks.py" in c["check_value"]
        for c in item["checks"]
    )


@pytest.mark.unit
def test_omn24874_contract_real_postgres_file_is_a_dod_command() -> None:
    commands = [
        c for c in _commands() if "test_omn24874_tenant_overlay_cost_real_postgres" in c
    ]
    assert commands, "no dod command names the real-Postgres file"
    # A run that skips any of its tests must fail the command.
    assert all("grep -q skipped" in c for c in commands)


# The criteria the review rounds' contracts no longer declare, with the evidence item that still reads each one. The repo's
# contract binds test (tests/ci/test_repo_evidence_caller_workflow.py) refuses a declared criterion no hosted test binds and refuses
# a bound local_done_gate item, so a criterion that only a PR comment, the ledger or a lab host can show is not declared; its reading
# stays as an unbound local_done_gate item that the local Done gate runs. Deleting such an item would delete the requirement.
_UNDECLARED_CRITERIA_EVIDENCE = {
    "OMN-24918": {
        "AC3": "dod-omn24918-ac3",
        "AC4": "dod-omn24918-ac4",
        "AC5": "dod-omn24918-ac5",
        "AC7": "dod-omn24918-ac7",
        "AC9": "dod-omn24918-ac9",
        "AC10": "dod-omn24918-ac10",
    },
    "OMN-24978": {"AC7": "dod-omn24978-ac7"},
    "OMN-25168": {"AC4": "dod-omn25168-ac4-lab-comment-names-head"},
    "OMN-25186": {"AC3": "dod-omn25186-ac3-comments-name-findings-and-head"},
}


@pytest.mark.unit
@pytest.mark.parametrize("ticket", sorted(_UNDECLARED_CRITERIA_EVIDENCE))
def test_a_criterion_a_review_contract_does_not_declare_keeps_its_unbound_local_reading(
    ticket: str,
) -> None:
    document = yaml.safe_load(
        (_CONTRACT.parent / f"{ticket}.yaml").read_text(encoding="utf-8")
    )
    declared = {
        ac["id"] for req in document["requirements"] for ac in req["acceptance"]
    }
    items = {item["id"]: item for item in document["dod_evidence"]}
    for criterion, item_id in _UNDECLARED_CRITERIA_EVIDENCE[ticket].items():
        assert criterion not in declared, (ticket, criterion)
        item = items[item_id]
        assert item["execution_scope"] == "local_done_gate", item_id
        assert not item.get("binds_ac"), item_id
        assert item["checks"], item_id
        # the item reads something: a command or a test the Done gate runs, not an empty shell.
        assert all(str(c["check_value"]).strip() for c in item["checks"]), item_id


@pytest.mark.unit
def test_omn24874_contract_ac3_check_reads_the_subject_only_not_a_body_line(
    tmp_path: Path,
) -> None:
    # Review finding 8: --grep matches the whole message, so a commit whose BODY has a line ending (#6335) was read as the fix.
    # A history whose only mention is a body line must fail the reading; the control (the default world) passes it.
    world = _Ac3World(tmp_path)
    assert world.run(_ac3_command()) == 0  # positive control: the subject form passes
    _git(world.repo, "checkout", "-q", "--orphan", "bodyonly")
    _git(
        world.repo,
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "OMN-24874: not the fix\n\nthis reverts the fix (#6335)",
    )
    _git(world.repo, "commit", "-q", "--allow-empty", "-m", "a later change")
    later = _git(world.repo, "rev-parse", "HEAD")
    tag = f"dev-20261117020000-{later[:12]}"
    _git(world.repo, "tag", tag, later)
    _git(world.repo, "update-ref", "refs/remotes/origin/dev", later)
    world.set_installed(later)
    world.set_runtime(commit=later, dev_merge_tag=tag)
    assert world.run(_ac3_command()) != 0
