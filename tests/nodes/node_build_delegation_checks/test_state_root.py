# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The build history is kept under the runtime's declared writable state root, never under the registry root.

The dev runtime container mounts the registry root (OMNI_HOME=/app) read-only and declares its writable state root in
ONEX_STATE_DIR / ONEX_STATE_ROOT (/app/data/.onex_state). The first build of the dev lane failed with
``PermissionError: [Errno 13] Permission denied: '/app/.onex_state'`` because the history path was derived from OMNI_HOME.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.config.state_root import OnexStateRootUnconfiguredError
from omnimarket.models.overlay_documents.model_build_delegation_checks_overlay import (
    ModelBuildDelegationChecksOverlay,
)
from omnimarket.nodes.node_build_delegation_checks.adapters.bus_sink import (
    BusRegressionSink,
)
from omnimarket.nodes.node_build_delegation_checks.adapters.file_build_store import (
    FileBuildStore,
)
from omnimarket.nodes.node_build_delegation_checks.handlers.handler_build_delegation_checks import (
    OMNI_HOME_ENV,
    REPLAY_ENV,
    HandlerBuildDelegationChecks,
)
from omnimarket.nodes.node_build_delegation_checks.models.model_build_delegation_checks import (
    EnumBuildChecksStatus,
    ModelBuildRecord,
    ModelRebuildCompletedObserved,
)
from omnimarket.nodes.node_build_delegation_checks.overlay import FILE_ENV
from omnimarket.nodes.node_build_delegation_checks.protocols.protocol_build_checks import (
    BuildStoreUnwritableError,
)
from tests.nodes.node_build_delegation_checks.test_bus_wiring import _git, _recorded


@pytest.fixture
def read_only_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, Path]]:
    """A registry root the process cannot write (the runtime's /app) and a separate writable state root."""
    home = tmp_path / "app"
    home.mkdir()
    clone = home / "omnimarket"
    clone.mkdir()
    _git(clone, "init", "-q")
    _git(clone, "commit", "-q", "--allow-empty", "-m", "first (#1)")
    green = _git(clone, "rev-parse", "HEAD")
    _git(clone, "commit", "-q", "--allow-empty", "-m", "second (#2)")
    red = _git(clone, "rev-parse", "HEAD")
    replay = tmp_path / "recorded.json"
    _recorded(replay, green, red)
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        yaml.safe_dump({"checks": ["C14", "C29"], "owner": "delegation-owner"}),
        encoding="utf-8",
    )
    state = tmp_path / "data" / ".onex_state"
    state.mkdir(parents=True)
    monkeypatch.setenv(OMNI_HOME_ENV, str(home))
    monkeypatch.setenv(REPLAY_ENV, str(replay))
    monkeypatch.setenv(FILE_ENV, str(overlay))
    monkeypatch.setenv("ONEX_STATE_DIR", str(state))
    monkeypatch.delenv("ONEX_STATE_ROOT", raising=False)

    async def swallow(
        self: BusRegressionSink, topic: str, key: str, value: bytes
    ) -> None:
        return None

    monkeypatch.setattr(BusRegressionSink, "_send", swallow)
    home.chmod(stat.S_IRUSR | stat.S_IXUSR)
    # only the registry root itself is closed to new entries; the clone inside stays readable for git
    try:
        yield home, state
    finally:
        home.chmod(stat.S_IRWXU)


def _shas(home: Path) -> tuple[str, str]:
    clone = home / "omnimarket"
    log = _git(clone, "log", "--format=%H", "--reverse").splitlines()
    return log[0], log[1]


def test_the_history_is_written_under_the_declared_state_root_with_the_registry_root_read_only(
    read_only_home: tuple[Path, Path],
) -> None:
    home, state = read_only_home
    assert not os.access(home, os.W_OK), (
        "the probe needs a registry root the process cannot write"
    )
    green, red = _shas(home)

    handler = HandlerBuildDelegationChecks()
    first = handler.handle(
        ModelRebuildCompletedObserved(git_sha=green, runtime_lane="dev")
    )
    second = handler.handle(
        ModelRebuildCompletedObserved(git_sha=red, runtime_lane="dev")
    )

    assert first.status != EnumBuildChecksStatus.ALREADY_RUN
    assert second.status != EnumBuildChecksStatus.ALREADY_RUN
    history = state / ModelBuildDelegationChecksOverlay().history_relpath
    assert history.is_file()
    assert [b["build_id"] for b in json.loads(history.read_text(encoding="utf-8"))] == [
        green,
        red,
    ]
    assert not (home / ".onex_state").exists()


def test_a_replayed_build_still_runs_nothing_from_the_state_root_history(
    read_only_home: tuple[Path, Path],
) -> None:
    home, _state = read_only_home
    _green, red = _shas(home)

    handler = HandlerBuildDelegationChecks()
    handler.handle(ModelRebuildCompletedObserved(git_sha=red, runtime_lane="dev"))
    replayed = HandlerBuildDelegationChecks().handle(
        ModelRebuildCompletedObserved(git_sha=red, runtime_lane="dev")
    )

    assert replayed.status == EnumBuildChecksStatus.ALREADY_RUN


def test_no_declared_state_root_refuses_instead_of_choosing_a_path(
    read_only_home: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _state = read_only_home
    _green, red = _shas(home)
    monkeypatch.delenv("ONEX_STATE_DIR", raising=False)
    monkeypatch.delenv("ONEX_STATE_ROOT", raising=False)

    with pytest.raises(OnexStateRootUnconfiguredError):
        HandlerBuildDelegationChecks().handle(
            ModelRebuildCompletedObserved(git_sha=red, runtime_lane="dev")
        )
    assert not (home / ".onex_state").exists()


@pytest.mark.parametrize(
    "bad", ["/etc/builds.json", "../builds.json", "a/../../b.json"]
)
def test_the_history_setting_is_a_path_inside_the_state_root(bad: str) -> None:
    with pytest.raises(ValueError, match="history_relpath"):
        ModelBuildDelegationChecksOverlay(history_relpath=bad)


def test_an_unwritable_state_root_is_refused_by_name_and_runs_no_check(
    read_only_home: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The known bad root: the declared state root sits under a regular file, so no directory can be made there (true for any
    user, root included). The handler answers with a named refusal; it does not raise and it does not run a probe."""
    home, _state = read_only_home
    _green, red = _shas(home)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    bad_root = blocker / ".onex_state"
    monkeypatch.setenv("ONEX_STATE_DIR", str(bad_root))

    result = HandlerBuildDelegationChecks().handle(
        ModelRebuildCompletedObserved(git_sha=red, runtime_lane="dev")
    )

    assert result.status == EnumBuildChecksStatus.REFUSED
    assert result.build_id == red
    assert str(bad_root) in result.reason
    assert "ONEX_STATE_DIR" in result.reason
    assert result.records == ()
    assert not (home / ".onex_state").exists()


def test_a_permission_error_on_the_state_root_is_refused_by_name_not_raised(
    read_only_home: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure the dev builds of 2026-11-24 raised: PermissionError making the history directory."""
    home, state = read_only_home
    _green, red = _shas(home)
    real_mkdir = Path.mkdir

    def deny(
        self: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
    ) -> None:
        if state in self.parents or self == state:
            raise PermissionError(13, "Permission denied", str(self))
        real_mkdir(self, mode, parents, exist_ok)

    monkeypatch.setattr(Path, "mkdir", deny)

    result = HandlerBuildDelegationChecks().handle(
        ModelRebuildCompletedObserved(git_sha=red, runtime_lane="dev")
    )

    assert result.status == EnumBuildChecksStatus.REFUSED
    assert "Permission denied" in result.reason
    assert str(state) in result.reason
    assert result.records == ()


def test_the_store_names_the_path_and_the_cause_when_it_cannot_write(
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    store = FileBuildStore(blocker / "state" / "builds.json", 5)
    record = ModelBuildRecord(
        build_id="a" * 40, runtime_lane="dev", started_at=datetime.now(UTC)
    )

    with pytest.raises(BuildStoreUnwritableError, match=r"builds\.json") as caught:
        store.begin(record)

    assert isinstance(caught.value, OSError)


def test_each_check_has_one_result_row_in_the_history_and_it_reads_back(
    read_only_home: tuple[Path, Path],
) -> None:
    """AC3: after a build on a writable state root, the history holds one row per configured check, read back through the store."""
    home, state = read_only_home
    green, red = _shas(home)
    handler = HandlerBuildDelegationChecks()
    handler.handle(ModelRebuildCompletedObserved(git_sha=green, runtime_lane="dev"))
    result = handler.handle(
        ModelRebuildCompletedObserved(git_sha=red, runtime_lane="dev")
    )

    history = state / ModelBuildDelegationChecksOverlay().history_relpath
    raw = json.loads(history.read_text(encoding="utf-8"))
    assert [row["check_id"] for row in raw[-1]["checks"]] == ["C14", "C29"]
    assert [r.check_id for r in result.records] == ["C14", "C29"]
    store = FileBuildStore(history, 200)
    for check_id in ("C14", "C29"):
        read_back = store.last_reading(check_id, "no-such-build")
        assert read_back is not None
        assert read_back.build_id == red


def _a_build() -> ModelBuildRecord:
    return ModelBuildRecord(
        build_id="a" * 40, runtime_lane="dev", started_at=datetime.now(UTC)
    )


def test_an_unreadable_history_is_named_by_the_store_not_raised_raw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read side of AC2: a history the process cannot read raised a bare PermissionError out of the store."""
    history = tmp_path / "builds.json"
    history.write_text("[]", encoding="utf-8")
    real_read = Path.read_text

    def deny(self: Path, encoding: str | None = None) -> str:
        if self == history:
            raise PermissionError(13, "Permission denied", str(self))
        return real_read(self, encoding=encoding)

    monkeypatch.setattr(Path, "read_text", deny)

    with pytest.raises(BuildStoreUnwritableError, match=r"builds\.json") as caught:
        FileBuildStore(history, 5).begin(_a_build())

    assert "Permission denied" in str(caught.value)
    assert "ONEX_STATE_DIR" in str(caught.value)


def test_a_history_that_is_not_valid_json_is_named_not_raised_raw(
    tmp_path: Path,
) -> None:
    history = tmp_path / "builds.json"
    history.write_text("{not json", encoding="utf-8")

    with pytest.raises(BuildStoreUnwritableError, match=r"builds\.json"):
        FileBuildStore(history, 5).previous_build("dev", "b" * 40)


def test_an_unreadable_history_refuses_the_build_by_name_and_runs_no_check(
    read_only_home: tuple[Path, Path],
) -> None:
    """The handler reads the previous build before it claims this one; a history that cannot be read is a named refusal."""
    home, state = read_only_home
    _green, red = _shas(home)
    history = state / ModelBuildDelegationChecksOverlay().history_relpath
    history.parent.mkdir(parents=True)
    history.write_text("{not json", encoding="utf-8")

    result = HandlerBuildDelegationChecks().handle(
        ModelRebuildCompletedObserved(git_sha=red, runtime_lane="dev")
    )

    assert result.status == EnumBuildChecksStatus.REFUSED
    assert str(history) in result.reason
    assert result.records == ()
