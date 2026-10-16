# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The variables-snapshot publisher: one complete read, one file, every lab host (OMN-19998).

The placement gate writes a dated snapshot for ONE repository with ``--write-variables-snapshot``;
a lab host reads a single ``PRRP_VARIABLES_SNAPSHOT`` file, so the publisher merges one read per
registry repository into one file and puts that file on every lab host. The gate is a stand-in
script here and ``ssh`` is a stand-in that runs the remote command in a per-host home, so the real
merge, the real atomic write and the real remote shell command run.
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_prrp_snapshot_publish_effect as publish_node
from omnimarket.nodes.node_prrp_snapshot_publish_effect import __main__ as publish_cli
from omnimarket.nodes.node_prrp_snapshot_publish_effect.handlers import (
    handler_prrp_snapshot_publish as publish,
)
from omnimarket.nodes.node_prrp_snapshot_publish_effect.models import (
    ModelPrrpSnapshotHost,
    ModelPrrpSnapshotPublishRequest,
)

HandlerPrrpSnapshotPublish = publish.HandlerPrrpSnapshotPublish
main = publish_cli.main

pytestmark = pytest.mark.unit

DESTINATION = ".omninode/prrp/variables-snapshot.json"
GATE = """\
    import argparse, json, os, sys
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--write-variables-snapshot", required=True)
    args = parser.parse_args()
    if os.environ.get("FAKE_GATE_CALLS"):
        with open(os.environ["FAKE_GATE_CALLS"], "a") as calls:
            calls.write(args.repo + "\\n")
    if args.repo in os.environ.get("FAKE_GATE_FAIL", "").split(","):
        print("HTTP 403: Resource not accessible by personal access token", file=sys.stderr)
        sys.exit(2)
    org = args.repo.split("/")[0]
    stamp = os.environ.get("FAKE_GATE_STAMP_" + args.repo.split("/")[1], "2026-10-14T12:00:00Z")
    body = {
        "schema": 1,
        "captured_at": stamp,
        "visibility": {args.repo: "private"},
        "scopes": {"org:" + org: {"RUNNER": "[\\"self-hosted\\"]"}, "repo:" + args.repo: {}},
    }
    open(args.write_variables_snapshot, "w").write(json.dumps(body))
    print("wrote", args.write_variables_snapshot)
"""
FAKE_SSH = """\
    import os, subprocess, sys
    args = sys.argv[1:]
    while args[0] == "-o":
        args = args[2:]
    target, body = args
    if target in os.environ.get("FAKE_SSH_DOWN", "").split(","):
        print("ssh: connect to host " + target + " port 22: Operation timed out", file=sys.stderr)
        sys.exit(255)
    home = os.path.join(os.environ["FAKE_SSH_ROOT"], target)
    os.makedirs(home, exist_ok=True)
    env = {**os.environ, "HOME": home}
    done = subprocess.run(["bash", "-c", body], env=env, stdin=sys.stdin.buffer)
    sys.exit(done.returncode)
"""
HOSTS = """\
    hosts:
      - name: host-a
        target: user@host-a.example.invalid
        local: true
      - name: host-b
        target: user@host-b.example.invalid
      - name: host-c
        target: user@host-c.example.invalid
      - name: host-d
        target: user@host-d.example.invalid
        available: false
        unavailable_reason: out of rotation
"""
REPOS = """\
    repositories:
      - {name: alpha, repo: OmniNode-ai/alpha}
      - {name: beta, repo: OmniNode-ai/beta}
      - {name: alpha_again, repo: OmniNode-ai/alpha}
"""


def _script(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> ModelPrrpSnapshotPublishRequest:
    (tmp_path / "hosts.yaml").write_text(textwrap.dedent(HOSTS))
    (tmp_path / "repos.yaml").write_text(textwrap.dedent(REPOS))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("FAKE_GATE_CALLS", str(tmp_path / "gate-calls"))
    monkeypatch.setenv("FAKE_SSH_ROOT", str(tmp_path / "remote"))
    monkeypatch.delenv("FAKE_GATE_FAIL", raising=False)
    monkeypatch.delenv("FAKE_SSH_DOWN", raising=False)
    return ModelPrrpSnapshotPublishRequest(
        gate_script=_script(tmp_path / "gate.py", GATE),
        repos_file=tmp_path / "repos.yaml",
        hosts=(
            ModelPrrpSnapshotHost(
                name="host-a", target="user@host-a.example.invalid", local=True
            ),
            ModelPrrpSnapshotHost(name="host-b", target="user@host-b.example.invalid"),
            ModelPrrpSnapshotHost(name="host-c", target="user@host-c.example.invalid"),
            ModelPrrpSnapshotHost(
                name="host-d",
                target="user@host-d.example.invalid",
                available=False,
                unavailable_reason="out of rotation",
            ),
        ),
        state_dir=tmp_path / "state",
        home=tmp_path / "home",
        destination=DESTINATION,
        python=Path(sys.executable),
        ssh_program=str(_script(tmp_path / "ssh", FAKE_SSH)),
    )


def _remote(tmp_path: Path, target: str) -> Path:
    return tmp_path / "remote" / target / DESTINATION


def test_one_run_writes_one_merged_fresh_file_on_every_available_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = HandlerPrrpSnapshotPublish().handle(_request(tmp_path, monkeypatch))
    assert result.exit_code == 0, result.failures
    assert (tmp_path / "gate-calls").read_text().splitlines() == [
        "OmniNode-ai/alpha",
        "OmniNode-ai/beta",
    ]
    local = tmp_path / "home" / DESTINATION
    merged = json.loads(local.read_text())
    assert merged["schema"] == 1
    assert merged["visibility"] == {
        "OmniNode-ai/alpha": "private",
        "OmniNode-ai/beta": "private",
    }
    assert sorted(merged["scopes"]) == [
        "org:OmniNode-ai",
        "repo:OmniNode-ai/alpha",
        "repo:OmniNode-ai/beta",
    ]
    for target in ("user@host-b.example.invalid", "user@host-c.example.invalid"):
        assert _remote(tmp_path, target).read_bytes() == local.read_bytes()
    assert not _remote(tmp_path, "user@host-d.example.invalid").exists()
    assert {outcome.host: outcome.status for outcome in result.hosts} == {
        "host-a": "written",
        "host-b": "written",
        "host-c": "written",
        "host-d": "skipped",
    }
    heartbeat = json.loads((tmp_path / "state" / "heartbeat.json").read_text())
    assert heartbeat["captured_at"] == merged["captured_at"]
    assert heartbeat["repos"] == 2  # the repeated repository is read once
    assert not (tmp_path / "state" / "failure.json").exists()


def test_the_file_is_dated_by_its_oldest_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_GATE_STAMP_beta", "2026-10-14T11:00:00Z")
    result = HandlerPrrpSnapshotPublish().handle(request)
    assert result.exit_code == 0
    merged = json.loads((tmp_path / "home" / DESTINATION).read_text())
    assert merged["captured_at"] == "2026-10-14T11:00:00Z"


def test_a_gate_that_cannot_read_one_repository_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path, monkeypatch)
    old = tmp_path / "home" / DESTINATION
    old.parent.mkdir(parents=True)
    old.write_text('{"previous": true}')
    monkeypatch.setenv("FAKE_GATE_FAIL", "OmniNode-ai/beta")
    result = HandlerPrrpSnapshotPublish().handle(request)
    assert result.exit_code == 1
    assert any(
        "OmniNode-ai/beta" in failure and "HTTP 403" in failure
        for failure in result.failures
    )
    assert old.read_text() == '{"previous": true}'  # a partial read replaces nothing
    assert not _remote(tmp_path, "user@host-b.example.invalid").exists()
    assert not (tmp_path / "state" / "heartbeat.json").exists()
    failure = json.loads((tmp_path / "state" / "failure.json").read_text())
    assert failure["failures"] == list(result.failures)


def test_an_unreachable_host_fails_the_run_loudly_and_the_others_still_get_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_SSH_DOWN", "user@host-b.example.invalid")
    result = HandlerPrrpSnapshotPublish().handle(request)
    assert result.exit_code == 1
    assert {outcome.host: outcome.status for outcome in result.hosts}[
        "host-b"
    ] == "failed"
    assert _remote(tmp_path, "user@host-c.example.invalid").is_file()
    assert not (
        tmp_path / "state" / "heartbeat.json"
    ).exists()  # no heartbeat unless every host has it
    assert (tmp_path / "state" / "failure.json").is_file()
    assert any(
        "host-b" in failure and "Operation timed out" in failure
        for failure in result.failures
    )


def test_a_remote_copy_that_does_not_read_back_identical_is_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path, monkeypatch)
    _script(
        tmp_path / "ssh",
        FAKE_SSH.replace(
            'done = subprocess.run(["bash", "-c", body], env=env, stdin=sys.stdin.buffer)',
            "body = body.replace('cat \"$HOME', 'echo stale; cat \"$HOME') if target == 'user@host-b.example.invalid' and 'mv -f' not in body"
            " else body\n"
            '    done = subprocess.run(["bash", "-c", body], env=env, stdin=sys.stdin.buffer)',
        ),
    )
    result = HandlerPrrpSnapshotPublish().handle(request)
    assert result.exit_code == 1
    assert any("read back" in failure for failure in result.failures)
    assert {outcome.host: outcome.status for outcome in result.hosts} == {
        "host-a": "written",
        "host-b": "failed",
        "host-c": "written",
        "host-d": "skipped",
    }
    assert (
        _remote(tmp_path, "user@host-c.example.invalid").read_bytes()
        == (request.home / DESTINATION).read_bytes()
    )
    assert not (request.state_dir / "heartbeat.json").exists()
    assert (request.state_dir / "failure.json").is_file()


def test_the_cli_prints_a_summary_and_exits_with_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    request = _request(tmp_path, monkeypatch)
    argv = _cli_arguments(request, tmp_path / "hosts.yaml")
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "prrp_variables_snapshot: OK" in out
    assert "host-b=written" in out
    monkeypatch.setenv("FAKE_GATE_FAIL", "OmniNode-ai/alpha")
    assert main(argv) == 1
    captured = capsys.readouterr()
    assert "ALARM prrp_variables_snapshot" in captured.err
    assert "OmniNode-ai/alpha" in captured.err


def test_a_missing_repos_file_or_an_empty_list_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path, monkeypatch)
    (tmp_path / "repos.yaml").write_text("repositories: []\n")
    result = HandlerPrrpSnapshotPublish().handle(request)
    assert result.exit_code == 1
    assert any("no repositories" in f for f in result.failures)
    (tmp_path / "repos.yaml").unlink()
    result = HandlerPrrpSnapshotPublish().handle(request)
    assert result.exit_code == 1
    assert any("repos.yaml" in f for f in result.failures)


def _cli_arguments(
    request: ModelPrrpSnapshotPublishRequest, hosts_file: Path
) -> list[str]:
    return [
        "--gate-script", str(request.gate_script),
        "--repos-file", str(request.repos_file),
        "--hosts-file", str(hosts_file),
        "--state-dir", str(request.state_dir),
        "--home", str(request.home),
        "--destination", DESTINATION,
        "--ssh-program", request.ssh_program,
    ]  # fmt: skip


@pytest.mark.parametrize(
    "document",
    [
        "",
        "[]",
        "{}",
        "hosts: null",
        "hosts: {}",
        "hosts: [null]",
        "hosts: [{name: host-a}]",
        "hosts: [{name: host-a, target: 42}]",
        "hosts: [{name: host-a, target: user@host-a.example.invalid, local: perhaps}]",
        "hosts: [{name: host-a, target: user@host-a.example.invalid, extra: true}]",
        "hosts: [",
    ],
)
def test_cli_refuses_malformed_hosts_before_running_the_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    document: str,
) -> None:
    request = _request(tmp_path, monkeypatch)
    hosts_file = tmp_path / "hosts.yaml"
    hosts_file.write_text(document)
    assert main(_cli_arguments(request, hosts_file)) == 2
    captured = capsys.readouterr()
    assert "ERROR: invalid hosts file" in captured.err
    assert "hosts.yaml" in captured.err
    assert not captured.out
    assert not (tmp_path / "gate-calls").exists()
    assert not request.state_dir.exists()
    assert not (request.home / DESTINATION).exists()


def test_module_cli_refuses_missing_hosts_file_with_exit_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path, monkeypatch)
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_prrp_snapshot_publish_effect",
            *_cli_arguments(request, tmp_path / "missing-hosts.yaml"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 2
    assert "ERROR: invalid hosts file" in done.stderr
    assert "missing-hosts.yaml" in done.stderr
    assert "Traceback" not in done.stderr
    assert not (tmp_path / "gate-calls").exists()


def test_the_effects_runtime_owns_the_publisher_command_subscription() -> None:
    contract_path = Path(publish_node.__file__).parent / "contract.yaml"
    contract = yaml.safe_load(contract_path.read_text())
    assert contract["descriptor"]["runtime_profiles"] == ["effects"]
