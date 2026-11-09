# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The publisher's target hosts are the declared lab host table's rows, and no file or flag names them (OMN-23396).

A planted ``lab_hosts`` overlay stands in for the table, the gate is a stand-in script and ``ssh`` runs the
remote command in a per-host home, so the real table reader, the real projection, the real merge and the real
remote shell command run.
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from omnimarket.nodes.node_prrp_snapshot_publish_effect import __main__ as publish_cli

pytestmark = pytest.mark.unit

DESTINATION = ".omninode/prrp/variables-snapshot.json"
GATE = """\
    import argparse, json
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--write-variables-snapshot", required=True)
    args = parser.parse_args()
    org = args.repo.split("/")[0]
    body = {
        "schema": 1,
        "captured_at": "2026-11-09T12:00:00Z",
        "visibility": {args.repo: "private"},
        "scopes": {"org:" + org: {}, "repo:" + args.repo: {}},
    }
    open(args.write_variables_snapshot, "w").write(json.dumps(body))
"""
# Records the options and destination of each call, then runs the remote command in a per-destination home.
FAKE_SSH = """\
    import os, subprocess, sys
    args = sys.argv[1:]
    options = []
    while args[0] in ("-o", "-J", "-i"):
        if args[0] != "-o":
            options += args[:2]
        args = args[2:]
    target, body = args
    with open(os.environ["FAKE_SSH_CALLS"], "a") as calls:
        calls.write(" ".join([target, *options]) + "\\n")
    if target in os.environ.get("FAKE_SSH_DOWN", "").split(","):
        print("ssh: connect to host " + target + " port 22: Operation timed out", file=sys.stderr)
        sys.exit(255)
    home = os.path.join(os.environ["FAKE_SSH_ROOT"], target)
    os.makedirs(home, exist_ok=True)
    done = subprocess.run(
        ["bash", "-c", body], env={**os.environ, "HOME": home}, stdin=sys.stdin.buffer
    )
    sys.exit(done.returncode)
"""
REPOS = """\
    repositories:
      - {name: alpha, repo: OmniNode-ai/alpha}
"""
TAIL = """\
    registry_root_candidates:
      - $HOME/registry
    bus_lane: test-bus
"""


def _script(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _table(rows: str) -> str:
    return (
        "hosts:\n"
        + textwrap.indent(textwrap.dedent(rows), "  ")
        + textwrap.dedent(TAIL)
    )


class _Run:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        (tmp_path / "repos.yaml").write_text(textwrap.dedent(REPOS))
        (tmp_path / "home").mkdir()
        self.gate = _script(tmp_path / "gate.py", GATE)
        self.ssh = _script(tmp_path / "ssh", FAKE_SSH)
        monkeypatch.setenv("FAKE_SSH_ROOT", str(tmp_path / "remote"))
        monkeypatch.setenv("FAKE_SSH_CALLS", str(tmp_path / "ssh-calls"))
        monkeypatch.delenv("FAKE_SSH_DOWN", raising=False)

    def publish(self, rows: str | None) -> int:
        environ: dict[str, str] = {}
        if rows is not None:
            overlay = self.tmp_path / "overlays" / "lab_hosts" / "overlay.yaml"
            overlay.parent.mkdir(parents=True, exist_ok=True)
            overlay.write_text(_table(rows))
            environ["ONEX_SKILL_OVERLAY_ROOTS"] = str(self.tmp_path / "overlays")
        return publish_cli.main(
            [
                "--gate-script", str(self.gate),
                "--repos-file", str(self.tmp_path / "repos.yaml"),
                "--state-dir", str(self.tmp_path / "state"),
                "--home", str(self.tmp_path / "home"),
                "--destination", DESTINATION,
                "--ssh-program", str(self.ssh),
            ],
            environ,
        )  # fmt: skip

    def written(self, target: str) -> bool:
        return (self.tmp_path / "remote" / target / DESTINATION).is_file()

    def calls(self) -> list[str]:
        path = self.tmp_path / "ssh-calls"
        return path.read_text().splitlines() if path.exists() else []


TWO_HOSTS = """\
    - name: h-one
      target: one.example.invalid
    - name: h-two
      target: two.example.invalid
"""
THREE_HOSTS = (
    TWO_HOSTS
    + """\
    - name: h-three
      target: three.example.invalid
"""
)
ONE_AND_THREE = """\
    - name: h-one
      target: one.example.invalid
    - name: h-three
      target: three.example.invalid
"""


def test_a_host_added_to_the_table_is_written_and_a_host_removed_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _Run(tmp_path, monkeypatch)
    assert run.publish(TWO_HOSTS) == 0, capsys.readouterr().err
    assert run.written("one.example.invalid")
    assert run.written("two.example.invalid")
    assert not run.written("three.example.invalid")

    # h-three is added to the table and h-two is removed from it: the next run follows the table.
    assert run.publish(ONE_AND_THREE) == 0, capsys.readouterr().err
    assert run.written("three.example.invalid")
    # h-two took one write and one read-back on the first run and no call on the second.
    assert [c.split()[0] for c in run.calls()].count("two.example.invalid") == 2
    heartbeat = json.loads((tmp_path / "state" / "heartbeat.json").read_text())
    assert heartbeat["hosts"] == {"h-one": "written", "h-three": "written"}
    assert "h-three=written" in capsys.readouterr().out


def test_a_row_the_table_marks_unavailable_is_skipped_with_no_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _Run(tmp_path, monkeypatch)
    rows = (
        TWO_HOSTS
        + """\
    - name: h-away
      target: away.example.invalid
      available: false
      unavailable_reason: out for repair
    - name: h-mac
      target: mac.example.invalid
      local: true
"""
    )
    assert run.publish(rows) == 0
    heartbeat = json.loads((tmp_path / "state" / "heartbeat.json").read_text())
    assert heartbeat["hosts"] == {
        "h-one": "written",
        "h-two": "written",
        "h-away": "skipped",
        "h-mac": "written",
    }
    assert not any(call.startswith("away.example.invalid") for call in run.calls())
    assert (
        tmp_path / "home" / DESTINATION
    ).is_file()  # the local row is a file write, no ssh
    assert not any(call.startswith("mac.example.invalid") for call in run.calls())


def test_a_row_carrying_the_real_tables_other_columns_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _Run(tmp_path, monkeypatch)
    rows = """\
    - name: h-full
      target: full.example.invalid
      codex: true
      docker: false
      cap: 12
      unit_slots: 3
      penalty: 0.25
      tenant_id: 00000000-0000-0000-0000-000000000000
      lane_binding: dev
      clones:
        - repo: OmniNode-ai/alpha
          ref: dev
          path: $OMNI_HOME/alpha
"""
    assert run.publish(rows) == 0
    assert run.written("full.example.invalid")


def test_the_row_login_and_a_fallback_route_carry_to_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _Run(tmp_path, monkeypatch)
    key = tmp_path / "id_lab"
    key.write_text("not a real key")
    monkeypatch.setenv("FAKE_SSH_DOWN", "jonah@direct.example.invalid")
    rows = f"""\
    - name: h-routed
      target: direct.example.invalid
      fallback_targets:
        - alias.example.invalid
      ssh_user: jonah
      ssh_identity: {key}
      fallback_proxy_jump: jump.example.invalid
"""
    assert run.publish(rows) == 0, capsys.readouterr().err
    assert run.calls() == [
        f"jonah@direct.example.invalid -i {key}",
        f"jonah@alias.example.invalid -J jump.example.invalid -i {key}",
        f"jonah@alias.example.invalid -J jump.example.invalid -i {key}",
    ]
    assert run.written("jonah@alias.example.invalid")


def test_a_destination_that_connected_and_failed_does_not_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _Run(tmp_path, monkeypatch)
    _script(
        tmp_path / "ssh",
        "import sys\nprint('Permission denied', file=sys.stderr)\nsys.exit(1)\n",
    )
    rows = """\
    - name: h-denied
      target: direct.example.invalid
      fallback_targets:
        - alias.example.invalid
"""
    assert run.publish(rows) == 1
    err = capsys.readouterr().err
    assert "ALARM prrp_variables_snapshot" in err
    assert "Permission denied" in err
    assert "alias.example.invalid" not in err


def test_every_host_down_raises_an_alarm_and_writes_no_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _Run(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_SSH_DOWN", "two.example.invalid")
    assert run.publish(TWO_HOSTS) == 1
    assert "ALARM prrp_variables_snapshot: h-two" in capsys.readouterr().err
    assert run.written("one.example.invalid")
    assert not (tmp_path / "state" / "heartbeat.json").exists()


def test_no_declared_table_refuses_before_any_gate_read_or_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _Run(tmp_path, monkeypatch)
    assert run.publish(None) == 2
    captured = capsys.readouterr()
    assert "ERROR: the lab host table cannot be read" in captured.err
    assert "ONEX_SKILL_OVERLAY_ROOTS" in captured.err
    assert not captured.out
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "home" / DESTINATION).exists()
    assert run.calls() == []


def test_a_table_with_an_invalid_row_refuses_without_naming_its_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _Run(tmp_path, monkeypatch)
    assert run.publish("    - name: h-bad\n      target: 42\n      cap: -1\n") == 2
    assert "ERROR: the lab host table cannot be read" in capsys.readouterr().err
    assert run.calls() == []
    assert not (tmp_path / "home" / DESTINATION).exists()


def test_the_command_takes_no_hosts_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _Run(tmp_path, monkeypatch)
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_prrp_snapshot_publish_effect",
            "--gate-script", str(tmp_path / "gate.py"),
            "--repos-file", str(tmp_path / "repos.yaml"),
            "--hosts-file", str(tmp_path / "hosts.yaml"),
            "--state-dir", str(tmp_path / "state"),
            "--home", str(tmp_path / "home"),
            "--destination", DESTINATION,
        ],
        capture_output=True,
        text=True,
        check=False,
    )  # fmt: skip
    assert done.returncode == 2
    assert "unrecognized arguments: --hosts-file" in done.stderr
    assert not (tmp_path / "state").exists()
