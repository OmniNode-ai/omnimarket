# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: every port failure is named in a typed result, none is raised or read as empty (OMN-20687)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.models.migration_sync import (
    ModelMigrationSyncAction,
    ModelMigrationSyncInventory,
)
from omnimarket.nodes.node_migration_sync_effect.handlers import (
    HandlerMigrationSyncApply,
    HandlerMigrationSyncRead,
)
from omnimarket.nodes.node_migration_sync_effect.handlers import (
    handler_migration_sync_read as read_module,
)
from omnimarket.nodes.node_migration_sync_effect.models import (
    ModelMigrationSyncApplyRequest,
    ModelMigrationSyncReadRequest,
)


def _read(tmp_path: Path, **override: object) -> ModelMigrationSyncInventory:
    fields: dict[str, object] = {
        "omnimarket_src": str(tmp_path),
        "allow_installed_package": False,
        "dest_root": str(tmp_path / "dest"),
        "manifest_path": str(tmp_path / "m.tsv"),
    }
    return HandlerMigrationSyncRead().handle(
        ModelMigrationSyncReadRequest.model_validate({**fields, **override})
    )


@pytest.mark.parametrize(
    "field", ["dest_root", "manifest_path", "omnimarket_src", "omni_home"]
)
def test_relative_paths_are_refused(field: str) -> None:
    fields = {"dest_root": "/d", "manifest_path": "/m", field: "relative/path"}
    with pytest.raises(ValidationError, match="absolute"):
        ModelMigrationSyncReadRequest.model_validate(fields)


def test_unknown_field_is_refused() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ModelMigrationSyncReadRequest.model_validate(
            {"dest_root": "/d", "manifest_path": "/m", "env": {"A": "b"}}
        )


def test_an_unresolvable_source_is_a_typed_unresolved_inventory(tmp_path: Path) -> None:
    inventory = _read(tmp_path)  # tmp_path has no src/omnimarket/nodes
    assert inventory.resolved is False
    assert inventory.failure == ""


def test_resolution_order_is_explicit_then_omni_home(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit" / "src" / "omnimarket" / "nodes"
    home = tmp_path / "home" / "omnimarket" / "src" / "omnimarket" / "nodes"
    explicit.mkdir(parents=True)
    home.mkdir(parents=True)
    both = _read(
        tmp_path,
        omnimarket_src=str(tmp_path / "explicit"),
        omni_home=str(tmp_path / "home"),
    )
    assert both.nodes_dir == str(explicit)
    only_home = _read(tmp_path, omnimarket_src="", omni_home=str(tmp_path / "home"))
    assert only_home.nodes_dir == str(home)
    # An explicit source with no nodes directory falls through, as the old script did.
    fallthrough = _read(
        tmp_path,
        omnimarket_src=str(tmp_path / "nowhere"),
        omni_home=str(tmp_path / "home"),
    )
    assert fallthrough.nodes_dir == str(home)


def test_the_installed_package_is_the_last_resort() -> None:
    inventory = HandlerMigrationSyncRead().handle(
        ModelMigrationSyncReadRequest(dest_root="/d", manifest_path="/m")
    )
    assert inventory.resolved is True
    assert inventory.nodes_dir.endswith(os.path.join("omnimarket", "nodes"))


def test_symlinked_source_and_vendored_files_are_not_files(tmp_path: Path) -> None:
    nodes = tmp_path / "src" / "omnimarket" / "nodes" / "node_a" / "migrations"
    nodes.mkdir(parents=True)
    real = tmp_path / "real.sql"
    real.write_text("x")
    (nodes / "0001.sql").symlink_to(real)
    dest = tmp_path / "dest" / "node_a"
    dest.mkdir(parents=True)
    (dest / "0002.sql").symlink_to(real)
    inventory = _read(tmp_path)
    assert inventory.source_files == ()
    assert inventory.vendored_files == ()


def test_an_unreadable_source_file_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nodes = tmp_path / "src" / "omnimarket" / "nodes" / "node_a" / "migrations"
    nodes.mkdir(parents=True)
    (nodes / "0001.sql").write_text("x")

    def deny(path: Path) -> bytes:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(read_module, "read_file_bytes", deny)
    inventory = _read(tmp_path)
    assert "0001.sql" in inventory.failure
    assert "Permission denied" in inventory.failure


def test_apply_refuses_a_path_that_escapes_the_vendored_tree_through_a_symlink(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "dest"
    outside = tmp_path / "outside"
    dest.mkdir()
    outside.mkdir()
    (dest / "node_a").symlink_to(outside)
    source = tmp_path / "s.sql"
    source.write_text("x")
    result = HandlerMigrationSyncApply().handle(
        ModelMigrationSyncApplyRequest(
            dest_root=str(dest),
            actions=(
                ModelMigrationSyncAction(
                    kind="copy",
                    relative_path="node_a/0001.sql",
                    source_path=str(source),
                ),
            ),
        )
    )
    assert result.ok is False
    assert result.applied == ()
    assert [f.relative_path for f in result.failed] == ["node_a/0001.sql"]
    assert "outside" in result.failed[0].reason
    assert not (outside / "0001.sql").exists()


def test_apply_names_a_missing_source_and_continues_with_the_rest(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "dest"
    good = tmp_path / "good.sql"
    good.write_text("g")
    result = HandlerMigrationSyncApply().handle(
        ModelMigrationSyncApplyRequest(
            dest_root=str(dest),
            actions=(
                ModelMigrationSyncAction(
                    kind="copy",
                    relative_path="n/missing.sql",
                    source_path=str(tmp_path / "gone.sql"),
                ),
                ModelMigrationSyncAction(
                    kind="copy", relative_path="n/good.sql", source_path=str(good)
                ),
            ),
        )
    )
    assert result.ok is False
    assert [a.relative_path for a in result.applied] == ["n/good.sql"]
    assert [f.relative_path for f in result.failed] == ["n/missing.sql"]
    assert (dest / "n" / "good.sql").read_text() == "g"


def test_removing_an_already_absent_file_is_applied_not_failed(tmp_path: Path) -> None:
    (tmp_path / "dest").mkdir()
    result = HandlerMigrationSyncApply().handle(
        ModelMigrationSyncApplyRequest(
            dest_root=str(tmp_path / "dest"),
            actions=(
                ModelMigrationSyncAction(kind="remove", relative_path="n/gone.sql"),
            ),
        )
    )
    assert result.ok is True
