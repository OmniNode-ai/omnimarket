# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22468: the four session compute nodes read and write only through ProtocolSessionFiles.

Each case injects a recording fake port and points the function at content the disk does not
hold (an absent file, or a file whose disk bytes differ from what the fake returns). A function
that still reads or writes the disk directly either fails on the absent file or returns the disk's
content, so each case fails; that is the node-purity gate's (OMN-13283) behaviour, proven per call.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from omnimarket.models.session_scope.model_session_scope import ModelSessionScope
from omnimarket.nodes.node_session_checkpoint_compute.handlers import checkpoint as ckpt
from omnimarket.nodes.node_session_checkpoint_compute.handlers import hook
from omnimarket.nodes.node_session_checkpoint_compute.handlers.handler_session_checkpoint import (
    HandlerSessionCheckpoint,
)
from omnimarket.nodes.node_session_checkpoint_compute.models.model_session_checkpoint import (
    ModelSessionCheckpointRequest,
)
from omnimarket.nodes.node_session_front_door_compute.handlers import (
    receipt,
    session_files,
)
from omnimarket.nodes.node_session_resume_compute.handlers import dangling
from omnimarket.nodes.node_team_identity_compute.handlers import (
    handler_team_identity as team,
)
from omnimarket.protocols.protocol_session_files import ProtocolSessionFiles


class FakeFiles:
    """Returns fixed content and records every path it was asked for."""

    def __init__(self, text: str = "", data: bytes = b"") -> None:
        self.text = text
        self.data = data
        self.reads: list[Path] = []
        self.writes: list[tuple[Path, str]] = []

    def read_text(
        self, path: Path, *, encoding: str = "utf-8", errors: str = "strict"
    ) -> str:
        self.reads.append(Path(path))
        return self.text

    def read_bytes(self, path: Path) -> bytes:
        self.reads.append(Path(path))
        return self.data

    def write_text(self, path: Path, data: str, *, encoding: str = "utf-8") -> None:
        self.writes.append((Path(path), data))


OPERATOR = ModelSessionScope(
    privileges="operator",
    entry_id=None,
    principals=(),
    writer_principal=None,
    diagnostic=None,
    source_sha256=None,
)


def test_fake_satisfies_the_port() -> None:
    assert isinstance(FakeFiles(), ProtocolSessionFiles)


def test_checkpoint_load_reads_through_the_port(tmp_path: Path) -> None:
    absent = tmp_path / "absent-checkpoint.json"
    fake = FakeFiles(text="not json")
    # Through the port the content is "not json" (refused); a direct disk read of the absent file
    # would raise InputError instead.
    with pytest.raises(ckpt.RefusedError):
        ckpt.load(absent, fs=fake)
    assert fake.reads == [absent]


def test_checkpoint_newest_reads_through_the_port(tmp_path: Path) -> None:
    on_disk = tmp_path / f"a{ckpt.SUFFIX}"
    on_disk.write_text("{}", encoding="utf-8")
    fake = FakeFiles(text="not json")
    assert ckpt.newest(tmp_path, OPERATOR, fs=fake) is None
    assert fake.reads == [on_disk]


def test_newest_prose_handoff_reads_through_the_port(tmp_path: Path) -> None:
    on_disk = tmp_path / f"handoff{ckpt.PROSE_SUFFIX}"
    on_disk.write_text("disk text", encoding="utf-8")
    fake = FakeFiles(text="principal: someone")
    assert ckpt.newest_prose_handoff(tmp_path, OPERATOR, fs=fake) == on_disk
    assert fake.reads == [on_disk]


def test_hook_env_values_read_through_the_port(tmp_path: Path) -> None:
    absent = tmp_path / "session.env"
    fake = FakeFiles(text="A=1\nB=2\n")
    assert hook._env_values(absent, fs=fake) == {"A": "1", "B": "2"}
    assert fake.reads == [absent]


def test_checkpoint_handler_reads_from_file_through_the_port(tmp_path: Path) -> None:
    absent = tmp_path / "start.json"
    fake = FakeFiles(text="not json")
    request = ModelSessionCheckpointRequest.model_validate(
        {
            "overlay_roots": [],
            "directory": str(tmp_path),
            "identity_claims": [],
            "operation": "write",
            "now": "2026-11-04T08:00:00Z",
            "write": {"lane": "sess-a", "from_file": str(absent)},
        }
    )
    result = HandlerSessionCheckpoint(fs=fake).handle(request)
    assert absent in fake.reads
    assert result.status == "refused"


def test_read_receipt_reads_through_the_port(tmp_path: Path) -> None:
    absent = tmp_path / "receipt.json"
    fake = FakeFiles(text="   ")
    # The port returns blank text, so the receipt is "empty"; a direct read of the absent file
    # would report it "unreadable".
    with pytest.raises(receipt.ReceiptError, match="is empty"):
        receipt.read_receipt(absent, fs=fake)
    assert fake.reads == [absent]


def test_assert_required_checks_reads_the_overlay_through_the_port(
    tmp_path: Path,
) -> None:
    absent = tmp_path / "overlay.yaml"
    content = b"overlay bytes"
    fake = FakeFiles(data=content)
    data = {
        "profile": "team",
        "overlay_sha256": hashlib.sha256(content).hexdigest(),
        "overlay_path": str(absent),
        "checks": {},
    }
    result = receipt.assert_required_checks(data, "team", fs=fake)
    assert absent in fake.reads
    assert "unreadable now" not in str(result.model_dump())


def test_session_files_init_writes_through_the_port(tmp_path: Path) -> None:
    fake = FakeFiles()
    session_files.init(tmp_path, "sess-a", "LANE=sess-a\n", dry_run=False, fs=fake)
    written = {path.name: body for path, body in fake.writes}
    assert written["session.env"] == "LANE=sess-a\n"
    assert "lanes.txt" in written
    assert not (tmp_path / "sess-a" / "session.env").exists()


def test_session_files_show_reads_through_the_port(tmp_path: Path) -> None:
    on_disk = tmp_path / "sess-a" / "session.env"
    on_disk.parent.mkdir()
    on_disk.write_text("DISK=1\n", encoding="utf-8")
    fake = FakeFiles(text="PORT=1\n")
    assert session_files.show(tmp_path, "sess-a", fs=fake) == ["PORT=1"]
    assert fake.reads == [on_disk]


def test_session_files_read_lanes_reads_through_the_port(tmp_path: Path) -> None:
    on_disk = tmp_path / "lanes.txt"
    on_disk.write_text("disk-lane dispatched\n", encoding="utf-8")
    fake = FakeFiles(text="port-lane dispatched\n")
    assert session_files.read_lanes(tmp_path, fs=fake) == [("port-lane", "dispatched")]
    assert fake.reads == [on_disk]


def test_dangling_read_ledger_reads_through_the_port(tmp_path: Path) -> None:
    absent = tmp_path / "ROLLING_WORK_LEDGER.md"
    fake = FakeFiles(text="")
    ledger = dangling.read_ledger(absent, fs=fake)
    assert fake.reads == [absent]
    assert ledger.recs == []


def test_team_identity_read_goes_through_the_port(tmp_path: Path) -> None:
    absent = tmp_path / "team_identity.yaml"
    fake = FakeFiles(data=b"declaration")
    assert team._read(absent, fs=fake) == b"declaration"
    assert fake.reads == [absent]


REPO = Path(__file__).resolve().parents[1]
SESSION_HANDLERS = (
    "src/omnimarket/nodes/node_session_checkpoint_compute/handlers/checkpoint.py",
    "src/omnimarket/nodes/node_session_checkpoint_compute/handlers/handler_session_checkpoint.py",
    "src/omnimarket/nodes/node_session_checkpoint_compute/handlers/hook.py",
    "src/omnimarket/nodes/node_session_front_door_compute/handlers/receipt.py",
    "src/omnimarket/nodes/node_session_front_door_compute/handlers/session_files.py",
    "src/omnimarket/nodes/node_session_resume_compute/handlers/dangling.py",
    "src/omnimarket/nodes/node_team_identity_compute/handlers/handler_team_identity.py",
)


def test_node_purity_gate_passes_on_the_four_session_nodes() -> None:
    """AC1: the repo's own node-purity hook (the pinned omnibase_core validator) passes the seven files.

    On omnimarket dev 63bc3719c the same hook reports 12 file-I/O violations in these files.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pre_commit",
            "run",
            "node-purity",
            "--files",
            *SESSION_HANDLERS,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    for relative in SESSION_HANDLERS:
        assert "node-purity-ok" not in (REPO / relative).read_text(encoding="utf-8"), (
            relative
        )
