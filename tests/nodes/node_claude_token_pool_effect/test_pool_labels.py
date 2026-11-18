# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pool publishes one line naming every account label: limit state, the reset it reads, the hosts on it (OMN-23000).

The pool is the real handler over a temporary store with fake tokens built at run time; the marks source
is a fake returning the source host's mirror; the login board is a real directory (the production adapter)
or a fake ssh runner. The golden case reads the line from the view; the failing control is the pool before
the board existed (no board: every label names no host, which is the line the operator could not read);
the production seam runs ``publish_labels`` end to end from a host's login file to the mirror it rewrites.
No test or result holds a token: the line carries labels, states, stamps and host names only.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from omnimarket.nodes.node_claude_token_pool_effect import (
    runtime_claude_pool_tick as tick_main,
)
from omnimarket.nodes.node_claude_token_pool_effect import (
    runtime_claude_token_pool as cli,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.adapter_rotation_ports import (
    DirectoryLoginBoard,
    FileTokenFile,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool_status_view import (
    HandlerClaudeTokenPoolStatusView,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool_status_view import (
    ModelPoolHostLogin,
    ModelPoolStatusViewRequest,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelRotationMark,
)
from tests.nodes.node_claude_token_pool_effect.test_status_view import (
    HOSTS,
    LABELS,
    OFF_OVERLAY_HOST,
    T0,
    Rig,
    fake_token,
    overlay,
)

pytestmark = pytest.mark.unit


def stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


class MemoryBoard:
    """A board holding records in memory; ``None`` records mean the board cannot be read."""

    def __init__(self, logins: tuple[ModelPoolHostLogin, ...] | None) -> None:
        self.logins = logins

    def publish(self, login: ModelPoolHostLogin) -> None:
        self.logins = (*(self.logins or ()), login)

    def read(self) -> tuple[ModelPoolHostLogin, ...] | None:
        return self.logins


def rig_with_marks(tmp: Path) -> Rig:
    rig = Rig(tmp)
    rig.source.marks = (
        ModelRotationMark(label="acct-b", until=stamp(T0 + timedelta(hours=8))),
        ModelRotationMark(label="caia", until=stamp(T0 + timedelta(days=5))),
        ModelRotationMark(label="onexbot", revoked=True),
    )
    return rig


def view_with(rig: Rig, board: MemoryBoard | None) -> HandlerClaudeTokenPoolStatusView:
    return HandlerClaudeTokenPoolStatusView(
        pool=rig.pool,
        overlay=overlay(),
        marks_source=rig.source,
        now=rig.clock,
        login_board=board,
    )


def test_golden_one_line_names_every_label_with_state_reset_and_hosts(
    tmp_path: Path,
) -> None:
    rig = rig_with_marks(tmp_path)
    board = MemoryBoard(
        (
            ModelPoolHostLogin(host="h053", label="acct-a", at=T0),
            ModelPoolHostLogin(host="h101", label="acct-a", at=T0),
            ModelPoolHostLogin(
                host="h201", label="caia", at=T0 - timedelta(seconds=800)
            ),
            ModelPoolHostLogin(host="h202", label=None, at=T0),
            # h106 has no record at all
        )
    )
    result = view_with(rig, board).handle(
        ModelPoolStatusViewRequest(host=OFF_OVERLAY_HOST)
    )
    assert result.basis == "source_marks"
    by_label = {row.label: row for row in result.label_states}
    assert tuple(by_label) == LABELS  # every label, in the pool's order
    assert by_label["acct-a"].state == "usable"
    assert by_label["acct-a"].reset is None
    assert by_label["acct-a"].hosts == ("h053", "h101")
    assert by_label["acct-b"].state == "limited"
    assert by_label["acct-b"].reset == stamp(T0 + timedelta(hours=8))
    assert by_label["acct-b"].hosts == ()
    assert by_label["caia"].state == "limited"
    assert by_label["caia"].hosts == (
        "h201",
    )  # 800 s old is inside three ticks of 300 s
    assert by_label["onexbot"].state == "revoked"
    assert by_label["onexbot"].reset is None
    assert result.hosts_unread == ("h106",)
    assert result.labels_line == (
        "claude-pool-labels basis=source_marks source=h053 marks_age_s=0 labels=4 logins_read=4/5 hosts_unread=h106"
        f" | acct-a:usable:reset=none:hosts=h053,h101 acct-b:limited:reset={stamp(T0 + timedelta(hours=8))}:hosts=none"
        f" caia:limited:reset={stamp(T0 + timedelta(days=5))}:hosts=h201 onexbot:revoked:reset=none:hosts=none"
    )
    assert "\n" not in result.labels_line
    assert fake_token("acct-a") not in result.model_dump_json()


def test_failing_control_without_the_board_no_label_names_a_host(
    tmp_path: Path,
) -> None:
    """The pool before the board: the states read, but no label says which hosts use it."""
    rig = rig_with_marks(tmp_path)
    result = view_with(rig, None).handle(
        ModelPoolStatusViewRequest(host=OFF_OVERLAY_HOST)
    )
    assert all(row.hosts == () for row in result.label_states)
    assert "logins_read=unreadable" in result.labels_line
    assert set(result.hosts_unread) == set(HOSTS)
    with_board = view_with(
        rig_with_marks(tmp_path / "again"),
        MemoryBoard((ModelPoolHostLogin(host="h101", label="caia", at=T0),)),
    ).handle(ModelPoolStatusViewRequest(host=OFF_OVERLAY_HOST))
    assert any(row.hosts for row in with_board.label_states)


def test_a_record_older_than_three_ticks_places_no_host(tmp_path: Path) -> None:
    rig = rig_with_marks(tmp_path)
    board = MemoryBoard(
        (ModelPoolHostLogin(host="h101", label="caia", at=T0 - timedelta(seconds=901)),)
    )
    result = view_with(rig, board).handle(
        ModelPoolStatusViewRequest(host=OFF_OVERLAY_HOST)
    )
    assert all(row.hosts == () for row in result.label_states)
    assert "h101" in result.hosts_unread


def test_unsynced_view_names_no_label_state(tmp_path: Path) -> None:
    rig = rig_with_marks(tmp_path)
    rig.source.error = "ssh exit 255"
    result = view_with(rig, MemoryBoard(())).handle(
        ModelPoolStatusViewRequest(host=OFF_OVERLAY_HOST)
    )
    assert result.basis == "unsynced_local_copy"
    assert result.label_states == ()
    assert "no label's limit state, reset or hosts is named" in result.labels_line


def test_directory_board_round_trip_and_a_record_holds_no_token(tmp_path: Path) -> None:
    board = DirectoryLoginBoard(home=tmp_path, directory="logins", ssh_target=None)
    assert board.read() == ()
    board.publish(ModelPoolHostLogin(host="h051", label="caia", at=T0))
    board.publish(ModelPoolHostLogin(host="h051", label="acct-a", at=T0))  # replaces
    board.publish(ModelPoolHostLogin(host="h202", label=None, at=T0))
    (tmp_path / "logins" / "junk.json").write_text("not json\n", encoding="utf-8")
    read = board.read()
    assert read is not None
    assert {(r.host, r.label) for r in read} == {("h051", "acct-a"), ("h202", None)}
    text = (tmp_path / "logins" / "h051.json").read_text(encoding="utf-8")
    assert json.loads(text) == {"host": "h051", "label": "acct-a", "at": stamp(T0)}


def test_directory_board_over_ssh_runs_one_command_per_call(tmp_path: Path) -> None:
    calls: list[Sequence[str]] = []

    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        out = json.dumps({"host": "h101", "label": "caia", "at": stamp(T0)}) + "\n"
        return subprocess.CompletedProcess(list(command), 0, out, "")

    board = DirectoryLoginBoard(
        home=tmp_path, directory="d/logins", ssh_target="hp-gen9", run=run
    )
    board.publish(ModelPoolHostLogin(host="h101", label="caia", at=T0))
    read = board.read()
    assert read == (ModelPoolHostLogin(host="h101", label="caia", at=T0),)
    assert [c[0] for c in calls] == ["ssh", "ssh"]
    assert all("hp-gen9" in c for c in calls)
    assert "h101.json" in calls[0][-1]
    assert not (tmp_path / "d").exists()  # nothing written locally

    def refused(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(command), 255, "", "")

    down = DirectoryLoginBoard(
        home=tmp_path, directory="d", ssh_target="hp-gen9", run=refused
    )
    assert down.read() is None
    with pytest.raises(OSError, match="remote exit 255"):
        down.publish(ModelPoolHostLogin(host="h101", label=None, at=T0))


def test_publish_labels_end_to_end_from_the_login_file_to_the_mirror(
    tmp_path: Path,
) -> None:
    """Production seam: the host's login file -> its label on the board -> the line and the mirror file."""
    rig = rig_with_marks(tmp_path)
    home = tmp_path / "home"
    login = FileTokenFile(home / ".config/omni/claude-oauth-token")
    login.write(SecretStr(fake_token("caia")))
    declared = overlay()
    result = cli.publish_labels("h101", overlay=declared, home=home, pool=rig.pool)
    assert result.basis == "local_pool"
    row = {r.label: r for r in result.label_states}
    assert row["caia"].hosts == ("h101",)
    assert row["acct-a"].hosts == ()
    mirror = json.loads((home / cli.LABELS_MIRROR).read_text(encoding="utf-8"))
    assert mirror["labels_line"] == result.labels_line
    assert {r["label"] for r in mirror["labels"]} == set(LABELS)
    assert fake_token("caia") not in json.dumps(mirror)
    board = (home / ".cache/omni" / cli.LOGIN_BOARD_NAME / "h101.json").read_text(
        encoding="utf-8"
    )
    assert fake_token("caia") not in board
    assert json.loads(board)["label"] == "caia"
    # a login the pool does not hold names no label and is not guessed
    login.write(SecretStr(fake_token("stranger")))
    again = cli.publish_labels("h101", overlay=declared, home=home, pool=rig.pool)
    assert all(r.hosts == () for r in again.label_states)


def test_tick_heartbeat_names_a_failed_publish_and_skips_a_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert tick_main._labels_fields("h101", dry_run=True) == {"labels": "dry_run"}

    def boom(host: str) -> None:
        raise OSError("login board write: ssh exit 255")

    monkeypatch.setattr(tick_main, "publish_labels", boom)
    assert tick_main._labels_fields("h101", dry_run=False) == {
        "labels": "publish_failed:OSError"
    }


def test_status_labels_verb_prints_the_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rig = rig_with_marks(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    result = view_with(rig, MemoryBoard(())).handle(
        ModelPoolStatusViewRequest(host=OFF_OVERLAY_HOST)
    )
    monkeypatch.setattr(cli, "run_status", lambda: result)
    assert cli.main(["status", "--labels"]) == 0
    printed = capsys.readouterr().out
    assert printed == result.labels_line + "\n"
    assert (tmp_path / "home" / cli.LABELS_MIRROR).exists()
    assert LABELS[0] in printed
