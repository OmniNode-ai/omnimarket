# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Archive before removal for the lane_results classes that declare it.

An entry that ages out is first copied outside the trees the pass removes, the copy is read back and verified, and only then
does the entry go: a dry run only names the archive path and writes nothing, a failed verify or an absent sync marker keeps
the entry, an unavailable archive root removes nothing, and an entry holding a symlink or a ``.git`` or ``memory`` folder is
never archived. The control without the declaration is the plain delete the archive consent forbids for these folders. Every
host, path and number below is fixture data.
"""

from __future__ import annotations

import os
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_host_retention_effect.handlers import host_retention_ports
from omnimarket.nodes.node_host_retention_effect.handlers.host_retention_ports import (
    FilesystemLaneReceipts,
    RetentionCleanupArchive,
)
from omnimarket.nodes.node_retention_cleanup.handlers.handler_file_retention import (
    load_file_policy,
)
from tests.nodes.node_host_retention_effect.host_retention_fakes import (
    DAY,
    NOW,
    FakeArchive,
    FakeClosure,
    entry,
    make_ports,
    results_class,
    run,
)

pytestmark = pytest.mark.unit


def kept_reasons(result: Any) -> dict[str, str]:
    return {Path(k.path).name: k.reason for k in result.kept}


def archive_stamp(days: float) -> str:
    """The UTC stamp of ``days`` before NOW, as the pass stamps an archive from the entry's newest mtime."""
    return datetime.fromtimestamp(NOW - days * DAY, tz=UTC).strftime("%Y%m%dT%H%M%SZ")


def archive_scene(
    tmp_path: Path,
) -> tuple[Path, Path, FakeClosure, dict[str, Any]]:
    """One old lane-omega entry whose lane the ledger closed ten days ago."""
    root = tmp_path / "remote-lanes"
    root.mkdir(parents=True)
    old = entry(root, "lane-omega", days=10)
    fields = {
        "age_classes": [
            results_class(
                root,
                keep_days=7,
                archive=True,
                lane_pattern="lane-(?P<lane>[a-z]+)",
            )
        ]
    }
    return root, old, FakeClosure({"omega": NOW - 10 * DAY}), fields


def expected_dest(tmp_path: Path) -> Path:
    """Where the scene's entry is archived: the archive root, the host, the class, the stamped name."""
    return (
        tmp_path
        / "archive"
        / "pool-a"
        / "receipts-a"
        / f"lane-omega.{archive_stamp(10)}.tar.gz"
    )


class ProbeArchive(FakeArchive):
    """Records whether the source entry still existed when it archived and verified it."""

    def __init__(self, root: Path | None = None) -> None:
        super().__init__(root)
        self.existed_at: dict[str, bool] = {}

    def archive(self, source: Path, dest: Path) -> int:
        self.existed_at["archive"] = source.exists()
        return super().archive(source, dest)

    def verify(self, source: Path, dest: Path) -> None:
        self.existed_at["verify"] = source.exists()
        super().verify(source, dest)


# ---------------------------------------------------------------------------------- dry run


def test_dry_run_names_the_archive_and_writes_nothing(tmp_path: Path) -> None:
    _root, old, closure, fields = archive_scene(tmp_path)
    archive = FakeArchive(tmp_path / "archive")

    result = run(
        tmp_path,
        fields,
        ports=make_ports(closure=closure, archive=archive),
        dry_run=True,
    )

    assert result.status == "ok"
    assert [i.archived_to for i in result.items] == [str(expected_dest(tmp_path))]
    assert result.items[0].finished_by == "ledger"
    assert not (tmp_path / "archive").exists()
    assert old.exists()


# ------------------------------------------------------------------------------- real run


def test_a_real_run_archives_and_verifies_before_it_removes(tmp_path: Path) -> None:
    _root, old, closure, fields = archive_scene(tmp_path)
    probe = ProbeArchive(tmp_path / "archive")

    result = run(tmp_path, fields, ports=make_ports(closure=closure, archive=probe))

    assert result.errors == ()
    assert not old.exists()
    assert probe.archived == [(old, expected_dest(tmp_path))]
    assert probe.verified == [(old, expected_dest(tmp_path))]
    assert probe.existed_at == {"archive": True, "verify": True}, (
        "the entry lived through archive and verify: the removal is the last step"
    )
    assert result.totals[0].archived_count == 1
    assert result.totals[0].removed_count == 1


# ------------------------------------------------------------------ the archive root is gone


def test_an_unavailable_archive_root_keeps_the_entry_and_a_dry_run_reports_once(
    tmp_path: Path,
) -> None:
    _root, old, closure, fields = archive_scene(tmp_path)
    archive = FakeArchive(None)

    result = run(tmp_path, fields, ports=make_ports(closure=closure, archive=archive))

    assert old.exists()
    assert kept_reasons(result) == {"lane-omega": "archive_unavailable"}
    assert archive.archived == []

    pair = tmp_path / "pair" / "remote-lanes"
    pair.mkdir(parents=True)
    omega = entry(pair, "lane-omega", days=10)
    beta = entry(pair, "lane-beta", days=10)
    fields = {
        "age_classes": [
            results_class(
                pair, keep_days=7, archive=True, lane_pattern="lane-(?P<lane>[a-z]+)"
            )
        ]
    }
    closure = FakeClosure({"omega": NOW - 10 * DAY, "beta": NOW - 10 * DAY})

    dry = run(
        tmp_path,
        fields,
        ports=make_ports(closure=closure, archive=FakeArchive(None)),
        dry_run=True,
    )

    assert [Path(i.path).name for i in dry.items] == ["lane-beta", "lane-omega"]
    assert dry.errors == (
        "receipts-a: archive root unavailable: a real run would remove nothing",
    )
    assert omega.exists()
    assert beta.exists()


# --------------------------------------------------------------------- the copy is not good


def test_a_failed_verify_leaves_the_entry_in_place(tmp_path: Path) -> None:
    _root, old, closure, fields = archive_scene(tmp_path)
    archive = FakeArchive(tmp_path / "archive", fail_verify=True)

    result = run(tmp_path, fields, ports=make_ports(closure=closure, archive=archive))

    assert old.exists()
    assert result.errors == (f"{old}: archive OSError",)
    assert result.totals[0].removed_count == 0


def test_an_unsynced_archive_holds_the_entry(tmp_path: Path) -> None:
    _root, old, closure, fields = archive_scene(tmp_path)
    archive = FakeArchive(tmp_path / "archive", synced=False)

    result = run(tmp_path, fields, ports=make_ports(closure=closure, archive=archive))

    assert old.exists()
    assert kept_reasons(result) == {"lane-omega": "pending_sync"}
    assert len(archive.archived) == 1


# ------------------------------------------------------------------ what may never be copied


@pytest.mark.parametrize("guard", ["symlink", ".git", "memory"])
def test_an_entry_holding_a_symlink_or_a_guarded_folder_is_never_archived(
    tmp_path: Path, guard: str
) -> None:
    _root, old, closure, fields = archive_scene(tmp_path)
    if guard == "symlink":
        os.symlink("elsewhere", old / "link")
    else:
        (old / guard).mkdir()
    archive = FakeArchive(tmp_path / "archive")

    result = run(tmp_path, fields, ports=make_ports(closure=closure, archive=archive))

    assert old.exists(), guard
    assert kept_reasons(result) == {"lane-omega": "not_archivable"}, guard
    assert archive.archived == []


def test_control_the_same_entry_without_it_is_archived(tmp_path: Path) -> None:
    _root, old, closure, fields = archive_scene(tmp_path)
    archive = FakeArchive(tmp_path / "archive")

    run(tmp_path, fields, ports=make_ports(closure=closure, archive=archive))

    assert not old.exists()
    assert [source for source, _dest in archive.archived] == [old]


# ---------------------------------------------------------------------------------- control


def test_control_without_the_archive_declaration_the_old_lane_is_plain_deleted(
    tmp_path: Path,
) -> None:
    """The same closed old lane with no archive consent: the age and ledger rules alone delete it
    and no archive call happens — the plain delete the archive consent forbids for these folders,
    which shows the ``archive: true`` declaration is what archives."""
    root = tmp_path / "plain" / "remote-lanes"
    root.mkdir(parents=True)
    old = entry(root, "lane-omega", days=10)
    fields = {
        "age_classes": [
            results_class(root, keep_days=7, lane_pattern="lane-(?P<lane>[a-z]+)")
        ]
    }
    archive = FakeArchive(tmp_path / "archive")

    run(
        tmp_path,
        fields,
        ports=make_ports(
            closure=FakeClosure({"omega": NOW - 10 * DAY}), archive=archive
        ),
    )

    assert not old.exists(), "with archive unset the pass deletes the entry outright"
    assert archive.archived == []
    assert archive.verified == []


# ------------------------------------------------------- golden and error chain, real adapters


def real_adapter_scene(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, dict[str, Any]]:
    """The real receipts and archive adapters over fixture trees.

    The lane the ledger window does not hold is judged by a real ``final`` receipt file, and the archive goes through
    node_retention_cleanup's own verified copy into the archive root its file policy names by environment variable, with
    the sync marker unset so a local copy counts as synced.
    """
    policy = load_file_policy()
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    monkeypatch.setenv(policy.archive_root_env, str(archive_root))
    monkeypatch.setattr(
        host_retention_ports,
        "load_file_policy",
        lambda: policy.model_copy(update={"synced_marker_xattr": ""}),
    )
    lanes = tmp_path / "remote-lanes"
    lanes.mkdir()
    old = entry(lanes, "rlane-omega-abc123", days=10, files=2)
    receipts_root = tmp_path / "remote-lanes-receipts"
    receipt = receipts_root / "omega" / "rlane-omega-abc123.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text('{"final": true}', encoding="utf-8")
    fields = {
        "age_classes": [
            results_class(
                lanes,
                keep_days=7,
                archive=True,
                lane_pattern="rlane-(?P<lane>[a-z]+)-(?P<run>[0-9a-f]{6})",
                receipt={
                    "root": str(receipts_root),
                    "glob": "{lane}/rlane-{lane}-{run}.json",
                    "dead_after_days": 30,
                },
            )
        ]
    }
    return old, archive_root, fields


def real_ports() -> Any:
    return make_ports(
        closure=FakeClosure({}),
        receipts=FilesystemLaneReceipts(),
        archive=RetentionCleanupArchive(),
    )


def test_golden_chain_the_real_adapters_archive_then_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, archive_root, fields = real_adapter_scene(tmp_path, monkeypatch)
    dest = (
        archive_root
        / "pool-a"
        / "receipts-a"
        / f"rlane-omega-abc123.{archive_stamp(10)}.tar.gz"
    )

    result = run(tmp_path, fields, ports=real_ports())

    assert result.status == "ok"
    assert result.errors == ()
    assert not old.exists()
    item = result.items[0]
    assert item.finished_by == "receipt_final"
    assert Path(item.archived_to) == dest
    assert dest.is_file()
    with tarfile.open(dest, "r:gz") as tar:
        assert sorted(
            member.name for member in tar.getmembers() if member.isfile()
        ) == [
            "rlane-omega-abc123/f0",
            "rlane-omega-abc123/f1",
        ]
    assert result.totals[0].archived_count == 1
    assert result.totals[0].removed_count == 1


def test_error_chain_a_colliding_archive_that_does_not_verify_keeps_the_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, archive_root, fields = real_adapter_scene(tmp_path, monkeypatch)
    dest = (
        archive_root
        / "pool-a"
        / "receipts-a"
        / f"rlane-omega-abc123.{archive_stamp(10)}.tar.gz"
    )
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"an earlier run left this; it is not this entry's archive")

    result = run(tmp_path, fields, ports=real_ports())

    assert old.exists()
    assert result.errors == (f"{old}: archive OSError",)
    assert result.totals[0].removed_count == 0
    assert result.totals[0].archived_count == 0
