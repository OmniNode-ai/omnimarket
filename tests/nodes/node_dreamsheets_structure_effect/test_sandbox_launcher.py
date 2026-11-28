# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bubblewrap profile, as flags (any OS). What the flags DO is tested on
Linux in ``test_sandbox_bwrap_linux.py``."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_dreamsheets_structure_effect.protocols.sandbox_launcher import (
    DREAMSHEETS_ARGS,
    SANDBOX_DOCUMENT_PATH,
    BwrapDreamsheetsLauncher,
    RunLayout,
    SystemLayout,
)

pytestmark = pytest.mark.unit

MERGED = SystemLayout({"/bin": "usr/bin", "/lib": "usr/lib", "/lib64": "usr/lib64"}, ())


def pairs(argv: list[str], flag: str, width: int) -> list[tuple[str, ...]]:
    return [tuple(argv[i + 1 : i + 1 + width]) for i, a in enumerate(argv) if a == flag]


def mounts(argv: list[str], flag: str) -> dict[str, str]:
    """``flag SRC DST`` occurrences as {SRC: DST}."""
    return {argv[i + 1]: argv[i + 2] for i, a in enumerate(argv) if a == flag}


def test_profile_isolates_everything(tmp_path: Path) -> None:
    layout = RunLayout(tmp_path / "run")
    launcher = BwrapDreamsheetsLauncher(tmp_path / "squashfs-root", system=MERGED)
    argv = launcher.command(layout)

    for flag in ("--unshare-all", "--die-with-parent", "--new-session", "--clearenv"):
        assert flag in argv, flag
    assert ("ALL",) in pairs(argv, "--cap-drop", 1)
    assert ("/",) in pairs(argv, "--remount-ro", 1)
    assert "--share-net" not in argv

    ro = mounts(argv, "--ro-bind")
    rw = mounts(argv, "--bind")
    assert ro[str(layout.input_file)] == SANDBOX_DOCUMENT_PATH
    assert ro[str(layout.config_dir)] == "/config"
    assert ro[str(tmp_path / "squashfs-root")] == "/opt/ds"
    assert rw == {str(layout.work_dir): "/work", str(layout.home_dir): "/home/s"}, (
        "nothing else is writable"
    )
    assert not any(
        dst in {"/tmp", "/run", "/home"} for dst in [*ro.values(), *rw.values()]
    )

    env = mounts(argv, "--setenv")
    assert set(env) == {"PATH", "HOME", "TMPDIR", "LANG", "LD_LIBRARY_PATH"}
    assert env["HOME"] == "/home/s"
    assert env["TMPDIR"] == "/work/tmp"
    assert launcher.environment(layout) == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}

    tail = argv[-len(DREAMSHEETS_ARGS) - 1 :]
    assert tail == ["/opt/ds/usr/bin/dreamsheets-desktop", *DREAMSHEETS_ARGS]
    assert "open_document,get_structure_summary" in DREAMSHEETS_ARGS
    assert "--read-only" in DREAMSHEETS_ARGS
    assert launcher.document_path(layout) == SANDBOX_DOCUMENT_PATH
    # The host path of the document never appears as a sandbox path.
    assert (
        str(layout.input_file)
        not in argv[argv.index("/opt/ds/usr/bin/dreamsheets-desktop") :]
    )


def test_non_merged_usr_hosts_bind_their_directories(tmp_path: Path) -> None:
    system = SystemLayout({}, ("/lib", "/bin"))
    argv = BwrapDreamsheetsLauncher(tmp_path, system=system).command(
        RunLayout(tmp_path)
    )
    ro = pairs(argv, "--ro-bind", 2)
    assert ("/lib", "/lib") in ro
    assert ("/bin", "/bin") in ro
    assert "--symlink" not in argv
