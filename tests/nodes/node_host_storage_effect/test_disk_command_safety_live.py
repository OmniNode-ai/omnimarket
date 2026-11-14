# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 (OMN-23867): entries outside the declared roots and entries in use are refused, with a live environment.

Live: real processes hold a cache entry (one by working directory, one by an open descriptor), a real linked git
worktree carries a lease, and symlinks and ``..`` lead out of the root. Each refused entry must still be there and
each free one must be gone. Red on a build that skips the root check or the in-use check (test_disk_command_controls).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_host_storage_effect.models.model_host_storage import (
    EnumHostStorageEntryOutcome,
    EnumHostStorageOutcome,
    EnumHostStorageRefusal,
)

from .disk_inputs import (
    by_entry,
    make_dirs,
    make_entry,
    real_handler,
    request,
    scenario_safety,
    scenario_worktrees,
)

pytestmark = pytest.mark.unit


def test_outside_root_and_in_use_entries_are_refused_and_the_free_ones_removed(
    tmp_path: Path,
) -> None:
    scenario_safety(tmp_path)


def test_a_real_worktree_with_a_lease_is_refused_and_a_free_one_removed(
    tmp_path: Path,
) -> None:
    scenario_worktrees(tmp_path)


def test_every_entry_refused_is_a_refused_result_and_nothing_is_removed(
    tmp_path: Path,
) -> None:
    dirs = make_dirs(tmp_path)
    secret = make_entry(dirs.outside, "secret")
    result = real_handler(dirs, no_holders=True).handle(
        request("evict_cache", secret, dirs.cache / "gone")
    )
    assert result.outcome is EnumHostStorageOutcome.REFUSED
    assert result.refusal is EnumHostStorageRefusal.ALL_ENTRIES_REFUSED
    assert by_entry(result) == {
        str(secret): EnumHostStorageEntryOutcome.REFUSED_OUTSIDE_ROOT,
        str(dirs.cache / "gone"): EnumHostStorageEntryOutcome.REFUSED_NOT_FOUND,
    }
    assert secret.exists()


def test_a_process_table_that_cannot_be_read_refuses_every_entry(
    tmp_path: Path,
) -> None:
    from omnimarket.nodes.node_host_storage_effect.handlers.backend_subprocess import (
        BackendHostStorageSubprocess,
    )
    from omnimarket.nodes.node_host_storage_effect.handlers.entry_remover_local import (
        EntryRemoverLocal,
    )
    from omnimarket.nodes.node_host_storage_effect.handlers.handler_host_storage import (
        HandlerHostStorage,
    )
    from omnimarket.nodes.node_host_storage_effect.models.model_host_storage_overlay import (
        ModelHostStorageOverlay,
    )

    from .disk_inputs import overlay_document

    dirs = make_dirs(tmp_path)
    entry = make_entry(dirs.cache, "pkg-a")
    overlay = ModelHostStorageOverlay.model_validate(overlay_document(dirs))
    handler = HandlerHostStorage(
        overlay,
        BackendHostStorageSubprocess(
            overlay.free_bytes_argv,
            overlay.free_bytes_timeout_seconds,
            EntryRemoverLocal(process_references=lambda: None),
        ),
    )
    result = handler.handle(request("evict_cache", entry))
    assert by_entry(result) == {str(entry): EnumHostStorageEntryOutcome.REFUSED_IN_USE}
    assert entry.exists()


def test_a_mount_point_inside_an_entry_stops_its_removal(tmp_path: Path) -> None:
    from unittest import mock

    dirs = make_dirs(tmp_path)
    entry = make_entry(dirs.cache, "pkg-a")
    real_lstat = Path.lstat

    class _Other:
        def __init__(self, base: object) -> None:
            self._base = base

        def __getattr__(self, name: str) -> object:
            if name == "st_dev":
                return self._base.st_dev + 1  # type: ignore[attr-defined]
            return getattr(self._base, name)

    def fake_lstat(self: Path, *args: object, **kwargs: object) -> object:
        info = real_lstat(self, *args, **kwargs)  # type: ignore[arg-type]
        return _Other(info) if self.name == "payload.bin" else info

    with mock.patch.object(Path, "lstat", fake_lstat):
        result = real_handler(dirs, no_holders=True).handle(
            request("evict_cache", entry)
        )
    assert by_entry(result) == {str(entry): EnumHostStorageEntryOutcome.FAILED}
    assert (entry / "payload.bin").exists()
