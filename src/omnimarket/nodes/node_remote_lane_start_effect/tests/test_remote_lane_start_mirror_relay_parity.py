# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity of the ssh transport's ``mirror_ledger`` and ``relay_reports`` steps with the frozen runner and relay-handler scripts (OMN-23425).

``mirror_ledger`` sends the host the recorded ledger-mirror text (bus or relock variant) with the runner's
``$LR`` prefix dropped, ``$LR/runs/<run>`` swapped for the context directory, and one inserted line that feeds the
staged CLAIM row to the script; ``relay_reports`` sends the relay handler's ``remote_script`` with `` | base64``
appended to its last line and reads the printed ``RELAY-TAR\n<base64>`` back as the ``relay_output_b64`` cell.
A change to either script fails these.
"""

from __future__ import annotations

import base64
import io
import re
import tarfile

import pytest

from omnimarket.nodes.node_lane_report_relay_effect.handlers.handler_lane_report_relay import (
    STAMP,
    tar_members,
)
from omnimarket.nodes.node_lane_report_relay_effect.handlers.handler_lane_report_relay import (
    remote_script as relay_remote_script,
)
from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_ssh_transport import (
    ModelHostCommandResult,
    SshLaneHostTransport,
)
from omnimarket.nodes.node_remote_lane_start_effect.tests.recorded_runner_steps import (
    RECORDED_MIRROR_RELOCK_SCRIPT,
    RECORDED_MIRROR_SCRIPT,
    RECORDED_RELAY_REPORTS_SCRIPT,
)
from omnimarket.nodes.node_remote_lane_start_effect.tests.test_remote_lane_start_staging_parity import (
    SNAPSHOT_OK,
    Recorder,
    as_recorded,
    stage,
    step,
    transport,
)

pytestmark = pytest.mark.unit

LEDGER = "example-ledger.md"
REGISTRY_ROOT = "/srv/example/registry"
LANE = "lane-example"
REPORT_NAME = "lane-example-2026-11-10T0900Z.md"
INSERTED = 'exec < "$C/claim-row.txt" || { echo "LEDGER-MIRROR failed"; exit 70; }'


def _mirror_script(via: str) -> str:
    """The recorded mirror text with the runner prefix dropped, the run root swapped, and the inserted line."""
    recorded = RECORDED_MIRROR_SCRIPT if via == "bus" else RECORDED_MIRROR_RELOCK_SCRIPT
    lines = as_recorded(recorded).splitlines()
    lines.insert(1, INSERTED)
    return "\n".join(lines)


def _remembering(runner: Recorder, via: str = "local") -> SshLaneHostTransport:
    """A transport that has staged the ledger snapshot, so it remembers the ledger name for the run."""
    ssh = transport(runner, via=via)
    ssh.stage_ledger_snapshot(stage(LEDGER, base64.b64encode(b"x").decode()))
    return ssh


MIRROR_OK = ModelHostCommandResult(exit_code=0, stdout="LEDGER-MIRROR ok\n")


@pytest.mark.parametrize("via", ["local", "bus"])
def test_mirror_script_equals_the_recorded_text_with_the_inserted_line(
    via: str,
) -> None:
    runner = Recorder(SNAPSHOT_OK)
    ssh = _remembering(runner, via)
    runner.answer = MIRROR_OK
    done = ssh.mirror_ledger(step())
    assert runner.argvs[-1][-1] == _mirror_script(via)
    assert done.ok is True
    assert done.cells == (("ledger_mirror", "ok"),)


def test_mirror_refuses_without_a_remembered_name_and_sends_no_host_command() -> None:
    runner = Recorder(SNAPSHOT_OK)
    done = transport(runner).mirror_ledger(step())
    assert done.ok is False
    assert "mirror_ledger" in (done.error or "")
    assert runner.argvs == []


def test_mirror_failure_when_stdout_says_failed() -> None:
    runner = Recorder(SNAPSHOT_OK)
    ssh = _remembering(runner)
    runner.answer = ModelHostCommandResult(
        exit_code=70, stdout="LEDGER-MIRROR failed\n"
    )
    done = ssh.mirror_ledger(step())
    assert done.ok is False
    assert done.error == "LEDGER-MIRROR failed"


def test_mirror_is_not_ok_when_the_host_exits_zero_without_the_printed_line() -> None:
    runner = Recorder(SNAPSHOT_OK)
    ssh = _remembering(runner)
    runner.answer = ModelHostCommandResult(exit_code=0, stdout="")
    assert ssh.mirror_ledger(step()).ok is False


TAIL_MARK = 'D="$H/.claude_scratch/reports"'


def _from_report_dir(script: str) -> list[str]:
    lines = script.splitlines()
    return lines[
        next(n for n, line in enumerate(lines) if line.startswith(TAIL_MARK)) :
    ]


def test_relay_script_is_the_handlers_script_piped_to_base64() -> None:
    runner = Recorder(SNAPSHOT_OK)
    done = transport(runner).relay_reports(step(registry_root=REGISTRY_ROOT))
    assert done.ok is True
    assert (
        runner.argvs[-1][-1] == relay_remote_script(REGISTRY_ROOT, LANE) + " | base64"
    )


def test_relay_script_gathers_the_reports_as_the_recorded_runner_script_did() -> None:
    """The head differs on purpose: the recorded text carries a candidate-root loop that never runs when the root is
    known (the handler's port keeps only the known-root line); everything from the report directory on is the recording."""
    runner = Recorder(SNAPSHOT_OK)
    transport(runner).relay_reports(step(registry_root=REGISTRY_ROOT))
    sent = runner.argvs[-1][-1]
    assert (
        _from_report_dir(sent)[:-1]
        == _from_report_dir(RECORDED_RELAY_REPORTS_SCRIPT)[:-1]
    )
    assert (
        _from_report_dir(sent)[-1]
        == _from_report_dir(RECORDED_RELAY_REPORTS_SCRIPT)[-1] + " | base64"
    )


def test_relay_round_trip_decodes_to_the_report_file() -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        data = b"report body\n"
        info = tarfile.TarInfo(REPORT_NAME)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    # the host prints the relay script's output through base64: the head line, then the tar bytes
    printed = base64.b64encode(b"RELAY-TAR\n" + buf.getvalue()).decode()
    runner = Recorder(ModelHostCommandResult(exit_code=0, stdout=printed + "\n"))
    done = transport(runner).relay_reports(step(registry_root=REGISTRY_ROOT))
    assert done.ok is True
    cell = dict(done.cells)["relay_output_b64"]
    _head, separator, tail = base64.b64decode(cell).partition(b"RELAY-TAR\n")
    assert separator
    found, too_large = tar_members(
        tail, re.compile(re.escape(LANE) + "-" + STAMP + r"\.md")
    )
    assert too_large == []
    assert {name: body for name, (body, _mtime) in found.items()} == {
        REPORT_NAME: b"report body\n"
    }


def test_relay_failure_names_the_host_failure() -> None:
    runner = Recorder(ModelHostCommandResult(exit_code=255, stderr="ssh: refused"))
    done = transport(runner).relay_reports(step(registry_root=REGISTRY_ROOT))
    assert done.ok is False
    assert done.error == "ssh: refused"


def test_relay_refuses_without_a_registry_root() -> None:
    runner = Recorder(SNAPSHOT_OK)
    done = transport(runner).relay_reports(step(registry_root=None))
    assert done.ok is False
    assert "relay_reports" in (done.error or "")
    assert runner.argvs == []
