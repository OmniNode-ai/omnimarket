# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review findings of omnimarket#5311 that were still true at dev's head (OMN-23069).

Each test names its finding and is red on the code before the fix. Every token is fake and built at run time.
"""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from omnimarket.nodes.node_claude_token_pool_effect import (
    runtime_claude_token_pool as cli,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.adapter_rotation_ports import (
    FileTokenFile,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    fingerprint,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.schedule import (
    RotationScheduler,
    load_schedule,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolRequest,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelClaudeTokenRotationRequest,
    ModelClaudeTokenRotationResult,
    ModelRotationMark,
    ModelRotationMarksSync,
)

from .test_claude_token_rotation_schedule import example_overlay
from .test_claude_token_rotation_tick import (
    LABELS,
    T0,
    FakeSource,
    Rig,
    fake_token,
    overlay,
)

pytestmark = pytest.mark.unit


def stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


# --- finding 1: a source snapshot must not erase a newer local observation -------------------------


def test_f1_a_newer_local_limit_survives_an_older_source_snapshot(
    tmp_path: Path,
) -> None:
    # the source wrote its (unmarked) snapshot 30 s ago; this host recorded the limit just now
    source = FakeSource(
        (ModelRotationMark(label="caia"),), at=T0 - timedelta(seconds=30)
    )
    rig = Rig(tmp_path, source=source)
    rig.login("caia")
    rig.limit("caia")
    result = rig.tick()
    assert result.outcome == "swapped"
    assert rig.in_use() not in (None, "caia")


def test_f1_a_newer_local_revocation_survives_an_older_source_snapshot(
    tmp_path: Path,
) -> None:
    source = FakeSource(
        (ModelRotationMark(label="onexbot"),), at=T0 - timedelta(seconds=30)
    )
    rig = Rig(tmp_path, source=source)
    rig.login("onexbot")
    rig.revoke("onexbot")
    result = rig.tick()
    assert result.outcome == "swapped"


def test_f1_an_older_local_limit_is_still_cleared_by_a_newer_source_snapshot(
    tmp_path: Path,
) -> None:
    source = FakeSource((ModelRotationMark(label="caia"),), at=T0)
    rig = Rig(tmp_path, source=source)
    rig.login("caia")
    rig.limit("caia")  # observed at T0 ...
    rig.clock.at = T0 + timedelta(minutes=2)  # ... and the source snapshot is newer
    source.at = T0 + timedelta(minutes=1)
    result = rig.tick()
    assert result.outcome == "kept"
    assert "caia" in result.marks_sync.changed


# --- finding 2: no login change without its event -------------------------------------------------


def test_f2_an_unwritable_event_journal_leaves_the_login_unchanged(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rig.login("caia")
    rig.limit("caia")
    blocker = rig.events.parent
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("a file where the journal directory should be", encoding="utf-8")
    with pytest.raises(OSError, match=r".+"):
        rig.tick()
    assert rig.in_use() == "caia"


# --- finding 3: a dry run decides on the same marks and writes nothing ----------------------------


def test_f3_a_dry_run_plans_the_swap_a_real_tick_makes_for_a_source_limit(
    tmp_path: Path,
) -> None:
    until = stamp(T0 + timedelta(hours=48))
    source = FakeSource((ModelRotationMark(label="caia", until=until),))
    rig = Rig(tmp_path, source=source)
    rig.login("caia")
    planned = rig.tick_handler.handle(
        ModelClaudeTokenRotationRequest(host=rig.host, dry_run=True)
    )
    assert planned.outcome == "planned"
    assert planned.from_label == "caia"
    assert rig.in_use() == "caia"


def test_f3_a_dry_run_does_not_rewrite_the_status_mirror_or_the_pool(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rig.login("caia")
    rig.limit("caia")
    mirror = rig.tmp / "cache" / "claude-token-pool.json"
    pool_file = rig.pool._pool_path
    mirror_before, pool_before = mirror.read_bytes(), pool_file.read_bytes()
    rig.clock.at = T0 + timedelta(minutes=5)  # a rewrite would change the mirror's time
    rig.tick_handler.handle(
        ModelClaudeTokenRotationRequest(host=rig.host, dry_run=True)
    )
    assert mirror.read_bytes() == mirror_before
    assert pool_file.read_bytes() == pool_before


# --- finding 4: uninstall keeps a failure to stop the schedule ------------------------------------


class Stub:
    def __init__(self, failing: str, code: int, err: str) -> None:
        self.failing, self.code, self.err = failing, code, err
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        if (
            command[0] == "launchctl"
            and self.failing == "bootout"
            and command[1] == "bootout"
        ):
            return subprocess.CompletedProcess(list(command), self.code, "", self.err)
        if "disable" in command and self.failing == "disable":
            return subprocess.CompletedProcess(list(command), self.code, "", self.err)
        return subprocess.CompletedProcess(list(command), 0, "", "")


def sched(tmp: Path, stub: Stub) -> RotationScheduler:
    return RotationScheduler(
        schedule=load_schedule(),
        overlay=example_overlay(),
        home=tmp / "home",
        run=stub,
        uid=501,
    )


def test_f4_a_rejected_launchd_bootout_is_not_reported_as_success(
    tmp_path: Path,
) -> None:
    stub = Stub("bootout", 5, "Boot-out failed: 5: Input/output error")
    s = sched(tmp_path, stub)
    s.plist.parent.mkdir(parents=True)
    s.plist.write_text("x", encoding="utf-8")
    done = s.uninstall("mac")
    assert done.exit_code == 5
    assert "Input/output error" in done.stderr
    assert s.plist.exists(), "the definition stays so the stop can be retried"


def test_f4_a_rejected_systemd_disable_is_not_overwritten_by_the_reload(
    tmp_path: Path,
) -> None:
    stub = Stub("disable", 1, "Failed to disable unit: Access denied")
    s = sched(tmp_path, stub)
    s.unit_dir.mkdir(parents=True)
    (s.unit_dir / s.timer_name).write_text("x", encoding="utf-8")
    done = s.uninstall("host-b")
    assert done.exit_code == 1
    assert "Access denied" in done.stderr


def test_f4_an_already_absent_job_still_uninstalls_clean(tmp_path: Path) -> None:
    launchd = sched(tmp_path, Stub("bootout", 3, "Boot-out failed: 3: No such process"))
    assert launchd.uninstall("mac").exit_code == 0
    systemd = sched(
        tmp_path,
        Stub("disable", 1, "Failed to disable unit: Unit file x.timer does not exist."),
    )
    assert systemd.uninstall("host-b").exit_code == 0


# --- finding 5: an unset OMNI_HOME refuses the tick -----------------------------------------------


@pytest.mark.parametrize("value", [None, ""])
def test_f5_an_unset_or_empty_omni_home_refuses_before_the_login_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    rig = Rig(tmp_path, host="host-b")
    rig.login("caia")
    rig.limit("caia")
    home = tmp_path / "home2"
    login = home / ".config" / "omni" / "claude-oauth-token"
    login.parent.mkdir(parents=True)
    login.write_text(fake_token("caia") + "\n", encoding="utf-8")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    if value is None:
        monkeypatch.delenv("OMNI_HOME", raising=False)
    else:
        monkeypatch.setenv("OMNI_HOME", value)
    with pytest.raises(ValueError, match="OMNI_HOME"):
        cli.run_tick("host-b", overlay=example_overlay(), home=home, pool=rig.pool)
    assert login.read_text().strip() == fake_token("caia")
    assert list(cwd.iterdir()) == []


# --- finding 6: staging and backup files are private even when they already exist -----------------


def test_f6_existing_permissive_staging_and_backup_files_are_made_private(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cfg" / "login"
    path.parent.mkdir()
    path.write_text(fake_token("caia") + "\n", encoding="utf-8")
    for name in ("login.prev", "login.new"):
        (path.parent / name).write_text("old\n", encoding="utf-8")
        (path.parent / name).chmod(0o644)
    FileTokenFile(path).write(SecretStr(fake_token("acct-a")))
    assert stat.S_IMODE((path.parent / "login.prev").stat().st_mode) == 0o600
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text().strip() == fake_token("acct-a")


# --- finding 7: a replaced candidate never leaves the login unmanaged -----------------------------


class ReplacingLoginFile:
    """The real login file, but the pool's label is replaced (``add --force``) just before the write lands."""

    def __init__(self, inner: FileTokenFile, replace) -> None:
        self._inner = inner
        self._replace = replace
        self.replaced = False

    def fingerprint(self) -> str | None:
        return self._inner.fingerprint()

    def write(self, value: SecretStr) -> None:
        if not self.replaced:
            self.replaced = True
            self._replace()
        self._inner.write(value)


def test_f7_a_label_replaced_during_the_swap_leaves_a_managed_login(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rig.login("caia")
    rig.limit("caia")
    new_token = "sk-ant-" + "oat01-" + "replacement" * 12

    def replace_every_candidate() -> None:
        for label in LABELS:
            if label != "caia":
                rig.pool.handle(
                    ModelClaudeTokenPoolRequest(
                        operation="add",
                        label=label,
                        setup_token_output=SecretStr(
                            new_token + label.replace("-", "")
                        ),
                        force=True,
                    )
                )

    wrapped = ReplacingLoginFile(rig.token_file, replace_every_candidate)
    rig.tick_handler._login = wrapped
    result = rig.tick()
    assert result.outcome == "swapped"
    held = {
        e.fingerprint
        for e in rig.pool.handle(ModelClaudeTokenPoolRequest(operation="status")).tokens
    }
    assert rig.token_file.fingerprint() in held, (
        "the login is a token the pool no longer manages"
    )


# --- finding 8: the CLI exit status keeps a refused marks sync ------------------------------------


@pytest.mark.parametrize("state", ["unreadable", "stale"])
def test_f8_a_refused_marks_sync_is_not_exit_zero(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    def fake_tick(
        host: str, *, dry_run: bool = False
    ) -> ModelClaudeTokenRotationResult:
        return ModelClaudeTokenRotationResult(
            host=host,
            rank=0,
            outcome="kept",
            in_use_state="usable",
            marks_sync=ModelRotationMarksSync(state=state, detail="refused"),
        )

    monkeypatch.setattr(cli, "run_tick", fake_tick)
    assert cli.main(["tick", "--host", "h"]) == cli.EXIT_EMPTY


def test_f8_a_healthy_sync_is_still_exit_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    def healthy_tick(
        host: str, *, dry_run: bool = False
    ) -> ModelClaudeTokenRotationResult:
        assert dry_run is False
        return ModelClaudeTokenRotationResult(
            host=host,
            rank=0,
            outcome="kept",
            in_use_state="usable",
            marks_sync=ModelRotationMarksSync(state="unchanged"),
        )

    monkeypatch.setattr(cli, "run_tick", healthy_tick)
    assert cli.main(["tick", "--host", "h"]) == 0


# --- finding 9: limit timestamps are validated and normalised -------------------------------------


@pytest.mark.parametrize("bad", ["tomorrow", "2026-11-09", "2026-11-09T22:00:00", ""])
def test_f9_a_limit_time_that_is_not_an_instant_is_refused(bad: str) -> None:
    with pytest.raises(ValidationError):
        ModelRotationMark(label="caia", until=bad)


def test_f9_an_offset_limit_time_is_normalised_to_utc() -> None:
    mark = ModelRotationMark(label="caia", until="2026-11-09T23:00:00+02:00")
    assert mark.until == "2026-11-09T21:00:00Z"
    assert (
        ModelRotationMark(label="caia", until="2026-11-09T21:00:00Z").until
        == "2026-11-09T21:00:00Z"
    )
    assert ModelRotationMark(label="caia").until is None


def test_f9_the_source_with_a_bad_limit_time_is_unreadable_not_applied(
    tmp_path: Path,
) -> None:
    from omnimarket.nodes.node_claude_token_pool_effect.handlers.adapter_rotation_ports import (
        MirrorMarksSource,
    )

    (tmp_path / "m.json").write_text(
        '{"at": "2026-11-07T14:00:00Z", "tokens": [{"label": "caia", "until": "tomorrow"}]}',
        encoding="utf-8",
    )
    read = MirrorMarksSource(
        home=tmp_path, mirror_path="m.json", ssh_target=None
    ).read()
    assert read.state == "unreadable"


_ = (UTC, os, fingerprint, overlay)
