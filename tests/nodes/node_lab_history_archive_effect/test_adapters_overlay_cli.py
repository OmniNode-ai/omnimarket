# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""rclone uploader argv and parsing, overlay resolution, the contract and the verb (OMN-17427)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from omnimarket.nodes.node_lab_history_archive_effect import cli as cli_module
from omnimarket.nodes.node_lab_history_archive_effect.adapters import (
    BusPartEventSink,
    RcloneDriveUploader,
    throttle_prefix,
)
from omnimarket.nodes.node_lab_history_archive_effect.models import (
    EnumLabHistoryArchivePartStatus,
    ModelArchiveBudget,
    ModelLabHistoryArchivePartEvent,
)
from omnimarket.nodes.node_lab_history_archive_effect.overlay import (
    OVERLAY_ENV,
    LabHistoryArchiveConfigurationError,
    load_deployment_overlay,
)
from omnimarket.nodes.node_lab_history_archive_effect.protocols import DriveUploadError

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).parents[3] / "src/omnimarket/nodes/node_lab_history_archive_effect"
)
BUDGET = ModelArchiveBudget(bytes_per_second=4_000_000, nice=19, io_class="idle")


class ScriptedRunner:
    def __init__(self, *replies: tuple[int, str, str]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[list[str], Mapping[str, str] | None]] = []

    def __call__(
        self,
        argv: Sequence[str],
        env: Mapping[str, str] | None = None,
        timeout: int = 1800,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), env))
        rc, out, err = self.replies.pop(0)
        return subprocess.CompletedProcess(list(argv), rc, out, err)


def uploader(runner: ScriptedRunner, *, rclone: bool = True) -> RcloneDriveUploader:
    return RcloneDriveUploader(
        remote="lab-archive",
        budget=BUDGET,
        runner=runner,
        which=lambda name: f"/usr/bin/{name}" if rclone or name != "rclone" else None,
        platform="linux",
    )


def test_throttle_prefix_lowers_cpu_and_io_priority_per_platform() -> None:
    def have(name: str) -> str:
        return f"/usr/bin/{name}"

    assert throttle_prefix(BUDGET, platform="linux", which=have) == (
        "nice",
        "-n",
        "19",
        "ionice",
        "-c",
        "3",
    )
    assert throttle_prefix(BUDGET, platform="darwin", which=have) == (
        "nice",
        "-n",
        "19",
        "taskpolicy",
        "-b",
    )
    assert throttle_prefix(BUDGET, platform="linux", which=lambda _: None) == (
        "nice",
        "-n",
        "19",
    )


def test_upload_is_niced_capped_single_stream_and_never_drive_for_desktop() -> None:
    runner = ScriptedRunner((0, "", ""))
    uploader(runner).upload(Path("/stage/p.tar.gz"), "Archive/h101/sessions", 4_000_000)
    argv, env = runner.calls[0]
    assert argv[:6] == ["nice", "-n", "19", "ionice", "-c", "3"]
    assert argv[6:9] == ["rclone", "copyto", "/stage/p.tar.gz"]
    assert argv[9] == "lab-archive:Archive/h101/sessions/p.tar.gz"
    assert argv[argv.index("--bwlimit") + 1] == "3906k"
    assert argv[argv.index("--transfers") + 1] == "1"
    assert env == {"GOMAXPROCS": "1"}
    assert not any("CloudStorage" in a or "GoogleDrive" in a for a in argv)


def test_upload_failure_raises_with_the_exit_code() -> None:
    with pytest.raises(DriveUploadError, match="exit 5"):
        uploader(ScriptedRunner((5, "", "quota exceeded"))).upload(
            Path("/s/p"), "d", 1000
        )


def test_remote_object_reads_drives_md5_and_size() -> None:
    listing = json.dumps(
        [
            {"Name": "other", "Size": 1, "Hashes": {"md5": "aa"}},
            {"Name": "p.tar.gz", "Size": 123, "Hashes": {"md5": "ABCDEF"}},
        ]
    )
    runner = ScriptedRunner((0, listing, ""), (0, "[]", ""))
    up = uploader(runner)
    found = up.remote_object("Archive/h101/sessions", "p.tar.gz")
    assert found is not None
    assert (found.size, found.md5) == (123, "abcdef")
    assert "--hash" in runner.calls[0][0]
    assert up.remote_object("Archive/h101/sessions", "gone") is None


def test_readiness_names_a_missing_binary_or_remote_without_reading_a_credential() -> (
    None
):
    assert uploader(ScriptedRunner(), rclone=False).check_ready() == (
        "UPLOADER_UNAVAILABLE",
        "rclone is not installed on this host",
    )
    code, detail = uploader(ScriptedRunner((0, "other:\n", ""))).check_ready() or (
        "",
        "",
    )
    assert code == "CREDENTIAL_MISSING"
    assert "lab-archive" in detail
    assert (
        uploader(ScriptedRunner((0, "other:\nlab-archive:\n", ""))).check_ready()
        is None
    )


EXAMPLE = {
    "hosts": [
        {"id": "h101", "hostnames": ["h101.lab"], "roles": ["lab"]},
        {"id": "h200", "roles": ["landing_controller"]},
    ],
    "sources": [
        {"name": "sessions", "root": "/data/claude/projects", "include": ["*.jsonl"]}
    ],
    "staging_dir": "/second/stage",
    "drive": {"remote": "lab-archive", "folder": "OmniNode Lab Archive"},
}


def test_overlay_pointer_wins_over_roots_and_invalid_overlays_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = tmp_path / "good.yaml"
    good.write_text(yaml.safe_dump(EXAMPLE))
    root = tmp_path / "roots" / "node_lab_history_archive_effect"
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text(
        yaml.safe_dump({**EXAMPLE, "staging_dir": "/elsewhere"})
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "roots"))
    monkeypatch.delenv(OVERLAY_ENV, raising=False)
    found = load_deployment_overlay()
    assert found is not None
    assert str(found.staging_dir) == "/elsewhere"
    monkeypatch.setenv(OVERLAY_ENV, str(good))
    found = load_deployment_overlay()
    assert found is not None
    assert str(found.staging_dir) == "/second/stage"
    assert found.refuse_roles == ("landing_controller",)
    assert found.schedule.timezone == "America/New_York"
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({**EXAMPLE, "surprise": 1}))
    monkeypatch.setenv(OVERLAY_ENV, str(bad))
    with pytest.raises(LabHistoryArchiveConfigurationError):
        load_deployment_overlay()
    monkeypatch.delenv(OVERLAY_ENV)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS")
    assert load_deployment_overlay() is None


def test_overlay_refuses_a_relative_root(tmp_path: Path) -> None:
    bad = tmp_path / "rel.yaml"
    bad.write_text(
        yaml.safe_dump({**EXAMPLE, "sources": [{"name": "s", "root": "relative/path"}]})
    )
    with pytest.raises(LabHistoryArchiveConfigurationError):
        load_deployment_overlay(bad)


def test_contract_declares_the_nightly_tick_the_topics_and_the_refused_role() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert {
        "topic": "onex.intent.platform.runtime-tick.v1",
        "operation": "lab_history_archive.scheduled_run",
    } in contract["input_subscriptions"]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.lab-history-archive-part.v1",
        "onex.evt.omnimarket.lab-history-archive-completed.v1",
    ]
    cfg = contract["config"]["lab_history_archive"]
    assert cfg["schedule"]["timezone"] == "America/New_York"
    assert (cfg["schedule"]["window_start"], cfg["schedule"]["window_end"]) == (
        "01:00",
        "05:00",
    )
    assert cfg["refuse_roles"] == ["landing_controller"]
    assert cfg["uploader"] == "rclone"
    assert cfg["retention_days"] == 7
    meta = yaml.safe_load((NODE / "metadata.yaml").read_text())
    assert meta["entry_points"]["onex.nodes"]["node_lab_history_archive_effect"]


def test_the_verb_is_registered_as_onex_lab_archive() -> None:
    pyproject = (Path(__file__).parents[3] / "pyproject.toml").read_text()
    assert (
        'lab-archive = "omnimarket.nodes.node_lab_history_archive_effect.cli:lab_archive_group"'
        in pyproject
    )


def test_the_verb_refuses_the_landing_controller_host_with_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = tmp_path / "o.yaml"
    overlay.write_text(yaml.safe_dump(EXAMPLE))
    monkeypatch.setattr("socket.gethostname", lambda: "h200")
    result = CliRunner().invoke(
        cli_module.lab_archive_group, ["run", "--overlay", str(overlay)]
    )
    assert result.exit_code == 2
    assert "refusal=HOST_IS_LANDING_CONTROLLER" in result.output
    assert "verdict=REFUSED" in result.output


def test_the_verb_dry_run_lists_parts_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    src = tmp_path / "boot" / "sessions"
    src.mkdir(parents=True)
    old = src / "a.jsonl"
    old.write_bytes(b"x" * 10)
    os.utime(old, (1.0, 1.0))
    overlay = tmp_path / "o.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                **EXAMPLE,
                "sources": [{"name": "sessions", "root": str(src)}],
                "staging_dir": str(tmp_path / "second-drive" / "stage"),
            }
        )
    )
    monkeypatch.setattr("socket.gethostname", lambda: "h101")
    monkeypatch.setattr(
        "omnimarket.nodes.node_lab_history_archive_effect.handlers.handler_lab_history_archive._device_of",
        lambda p: 2 if "second-drive" in p.parts else 1,
    )
    result = CliRunner().invoke(
        cli_module.lab_archive_group, ["run", "--dry-run", "--overlay", str(overlay)]
    )
    assert result.exit_code == 0, result.output
    assert "status=DRY_RUN" in result.output
    assert "verdict=DRY_RUN" in result.output
    assert old.exists()


def test_the_part_sink_publishes_on_the_topic_the_contract_declares() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    sent: list[tuple[str, bytes, bytes]] = []
    event = ModelLabHistoryArchivePartEvent(
        host="h101",
        source="sessions",
        part="sessions-20261017T0600Z-001.tar.gz",
        status=EnumLabHistoryArchivePartStatus.ARCHIVED,
        files=2,
        bytes_source=7000,
        bytes_freed=7000,
    )
    BusPartEventSink(
        lambda t, k, v: sent.append((t, k, v)),
        contract["event_bus"]["publish_topics"][0],
    ).emit(event)
    sent_topic, key, value = sent[0]
    assert sent_topic == contract["event_bus"]["publish_topics"][0]
    assert sent_topic.endswith("lab-history-archive-part.v1")
    assert key == b"h101:sessions-20261017T0600Z-001.tar.gz"
    assert json.loads(value)["bytes_freed"] == 7000
