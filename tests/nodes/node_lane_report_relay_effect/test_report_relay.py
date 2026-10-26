# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Relay a lab lane's report to the launching host (OMN-17427), entirely offline.

A lane writes its report under the running host's ``$ONEX_REGISTRY_ROOT/.claude_scratch/reports``, so a lane placed on
another host left a report nobody on the launching host could read. At close the runner relays
``<lane>-<stamp>.md`` from the host into the launching host's own reports directory, the same way it relays
delegation receipts.

Run from the skill directory: python3 -B -m pytest -q -p no:cacheprovider tests/test_report_relay.py
The fake ssh executes the actual bash and tar against temporary remote trees.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

PKG = "omnimarket.nodes.node_lane_report_relay_effect"
handler = importlib.import_module(f"{PKG}.handlers.handler_lane_report_relay")
ModelLaneReportRelayRequest = importlib.import_module(
    f"{PKG}.models.model_lane_report_relay"
).ModelLaneReportRelayRequest
NodeLaneReportRelayEffect = importlib.import_module(PKG).NodeLaneReportRelayEffect
pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def overlay_roots(tmp_path, monkeypatch):
    import yaml

    root = tmp_path / "overlays/lab_hosts"
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "hosts": [{"name": "host-a", "target": "192.0.2.1"}],
                "registry_root_candidates": ["$HOME/registry"],
            }
        )
    )
    monkeypatch.delenv("ONEX_REGISTRY_ROOT", raising=False)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root.parent))


LANE = "deleg-exp-7"
NAME = f"{LANE}-2026-10-18T1945Z.md"
NOW = datetime(2026, 10, 18, 20, 30, tzinfo=UTC)


class FakeSSH:
    def __init__(self, root):
        self.root = root
        self.calls = []

    def __call__(self, argv, **kw):
        assert argv[0] == "ssh"
        assert kw == {"capture_output": True, "check": False, "timeout": 180}
        self.calls.append(argv)
        env = {**os.environ, "HOME": str(self.root)}
        env.pop("ONEX_REGISTRY_ROOT", None)
        return subprocess.run(["bash", "-c", argv[-1]], env=env, **kw)


def remote_reports(root):
    home = root / "registry"
    (home / "omnibase_infra").mkdir(parents=True, exist_ok=True)
    directory = home / ".claude_scratch" / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    return home, directory


def local_reports(tmp_path):
    return tmp_path / "local" / ".claude_scratch" / "reports"


def invoke(tmp_path, fake, **over):
    args = {
        "target": "fake",
        "remote_registry_root": None,
        "lane": LANE,
        "local_registry_root": str(tmp_path / "local"),
        "host_name": "lab-a",
        "result_text": "",
        "run": fake,
        "now": NOW,
    }
    args.update(over)
    return (
        NodeLaneReportRelayEffect()
        .handle(ModelLaneReportRelayRequest(**args))
        .as_dict()
    )


def test_the_lanes_report_is_copied_byte_for_byte_in_one_ssh(tmp_path):
    _, directory = remote_reports(tmp_path / "remote")
    body = "# report\nVerdict: done\né\n".encode()
    (directory / NAME).write_bytes(body)
    os.utime(directory / NAME, (1700000000, 1700000000))
    fake = FakeSSH(tmp_path / "remote")
    got = invoke(tmp_path, fake)
    assert got == {
        "copied": [NAME],
        "already_local": [],
        "missing": [],
        "from_result": None,
        "error": None,
    }
    assert len(fake.calls) == 1
    dest = local_reports(tmp_path) / NAME
    assert dest.read_bytes() == body
    assert dest.stat().st_mtime == 1700000000
    assert sorted(p.name for p in local_reports(tmp_path).iterdir()) == [
        NAME
    ]  # no temp file left behind


def test_only_this_lanes_reports_are_copied(tmp_path):
    _, directory = remote_reports(tmp_path / "remote")
    (directory / NAME).write_text("mine\n")
    (directory / "deleg-exp-70-2026-10-18T1945Z.md").write_text(
        "a lane whose name starts the same\n"
    )
    (directory / f"{LANE}-notes.md").write_text("not a report stamp\n")
    (directory / "other-2026-10-18T1945Z.md").write_text("another lane\n")
    got = invoke(tmp_path, FakeSSH(tmp_path / "remote"))
    assert got["copied"] == [NAME]
    assert got["error"] is None
    assert [p.name for p in local_reports(tmp_path).iterdir()] == [NAME]


def test_every_report_of_a_resumed_lane_is_copied(tmp_path):
    _, directory = remote_reports(tmp_path / "remote")
    second = f"{LANE}-2026-10-18T2015Z.md"
    (directory / NAME).write_text("first\n")
    (directory / second).write_text("second\n")
    got = invoke(tmp_path, FakeSSH(tmp_path / "remote"))
    assert got["copied"] == [NAME, second]


def test_an_existing_local_report_is_never_overwritten(tmp_path):
    _, directory = remote_reports(tmp_path / "remote")
    (directory / NAME).write_text("from the host\n")
    local = local_reports(tmp_path)
    local.mkdir(parents=True)
    (local / NAME).write_text("already here\n")
    got = invoke(tmp_path, FakeSSH(tmp_path / "remote"))
    assert got == {
        "copied": [],
        "already_local": [NAME],
        "missing": [],
        "from_result": None,
        "error": None,
    }
    assert (local / NAME).read_text() == "already here\n"


def test_a_symlink_named_like_a_report_is_not_followed(tmp_path):
    _, directory = remote_reports(tmp_path / "remote")
    secret = tmp_path / "remote" / "secret.txt"
    secret.write_text("not a report\n")
    (directory / NAME).symlink_to(secret)
    got = invoke(tmp_path, FakeSSH(tmp_path / "remote"))
    assert got["copied"] == []
    assert got["error"] is None
    assert not local_reports(tmp_path).exists() or not list(
        local_reports(tmp_path).iterdir()
    )


def test_an_oversized_report_is_missing_not_copied(tmp_path, monkeypatch):
    _, directory = remote_reports(tmp_path / "remote")
    (directory / NAME).write_bytes(b"x" * 64)
    monkeypatch.setitem(handler.POLICY, "report_max_bytes", 16)
    got = invoke(tmp_path, FakeSSH(tmp_path / "remote"))
    assert got["copied"] == []
    assert got["missing"] == [NAME]
    assert not local_reports(tmp_path).exists() or not list(
        local_reports(tmp_path).iterdir()
    )


@pytest.mark.parametrize("lane", ["", "a b", "x;rm -rf /", "../up", "l\nx"])
def test_a_lane_name_that_is_not_a_plain_token_never_reaches_a_shell(tmp_path, lane):
    fake = FakeSSH(tmp_path / "remote")
    got = invoke(tmp_path, fake, lane=lane)
    assert got["copied"] == []
    assert got["error"]
    assert not fake.calls


@pytest.mark.parametrize("kind", ["rc", "timeout", "oserror"])
def test_a_transport_error_is_recorded_and_raises_nothing(tmp_path, kind):
    def fake_run(argv, **kw):
        if kind == "timeout":
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        if kind == "oserror":
            raise OSError("transport unavailable")
        return subprocess.CompletedProcess(argv, 255, b"", b"offline")

    got = invoke(tmp_path, fake_run)
    assert got["copied"] == []
    assert got["error"]


def test_a_host_with_no_registry_root_is_an_error(tmp_path):
    def fake_run(
        argv, **kw
    ):  # the script's own answer when no candidate root holds a registry
        return subprocess.CompletedProcess(argv, 0, b"RELAY-NO-HOME\n", b"")

    got = invoke(tmp_path, fake_run)
    assert got["copied"] == []
    assert got["error"] == "no registry root on the host"


def test_the_script_finds_the_root_under_home_when_none_is_named(tmp_path):
    _, directory = remote_reports(
        tmp_path / "remote"
    )  # <HOME>/registry_root, the second candidate
    (directory / NAME).write_text("found by discovery\n")
    got = invoke(tmp_path, FakeSSH(tmp_path / "remote"))
    assert got["copied"] == [NAME]


def test_a_lane_that_wrote_no_report_leaves_its_final_message_as_the_report(tmp_path):
    remote_reports(tmp_path / "remote")
    got = invoke(
        tmp_path,
        FakeSSH(tmp_path / "remote"),
        result_text="LANE_RESULT outcome=partial\nDid half.",
    )
    name = f"{LANE}-2026-10-18T2030Z.md"
    assert got["copied"] == []
    assert got["from_result"] == name
    assert got["error"] is None
    text = (local_reports(tmp_path) / name).read_text()
    assert "final message" in text.splitlines()[0]
    assert "lab-a" in text.splitlines()[0]
    assert text.endswith("LANE_RESULT outcome=partial\nDid half.\n")


def test_the_final_message_does_not_replace_a_report_that_arrived(tmp_path):
    _, directory = remote_reports(tmp_path / "remote")
    (directory / NAME).write_text("the real report\n")
    got = invoke(tmp_path, FakeSSH(tmp_path / "remote"), result_text="short summary")
    assert got["copied"] == [NAME]
    assert got["from_result"] is None
    assert [p.name for p in local_reports(tmp_path).iterdir()] == [NAME]


def test_the_final_message_is_kept_when_the_host_cannot_be_reached(tmp_path):
    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 255, b"", b"offline")

    got = invoke(tmp_path, fake_run, result_text="the only copy")
    assert got["error"]
    assert got["from_result"] == f"{LANE}-2026-10-18T2030Z.md"


def test_an_empty_final_message_writes_nothing(tmp_path):
    remote_reports(tmp_path / "remote")
    got = invoke(tmp_path, FakeSSH(tmp_path / "remote"), result_text="  \n")
    assert got["from_result"] is None
    assert not local_reports(tmp_path).exists() or not list(
        local_reports(tmp_path).iterdir()
    )


def test_the_contract_names_the_node_and_its_handler():
    contract = json.loads(
        (Path(handler.__file__).resolve().parents[1] / "contract.yaml").read_text()
    )
    assert contract["name"] == "node_lane_report_relay_effect"
    assert contract["node_type"] == "EFFECT_GENERIC"
    assert contract["handler"]["module"] == handler.__name__
    assert contract["handler"]["class"] == "HandlerLaneReportRelay"
    assert contract["entrypoint"]["module"] == PKG
