# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How the DreamSheets binary is started: the run layout, the launcher
protocol, and the bubblewrap launcher this node ships with.

The launcher only BUILDS the command and environment; the handler spawns it.
That keeps the whole sandbox profile a pure function of the layout, so its
flags are unit-tested on any OS, and the only launcher in this package is the
sandboxed one (tests bring their own direct launcher).

Inside the sandbox every path is fixed, so nothing from the host -- the
document's real path or name, the user's home -- is visible to the binary:

====================  ===================================================
``/opt/ds``           the extracted DreamSheets AppImage, read-only
``/in/doc.dsheet``    the hashed copy of the input, read-only
``/config``           an empty config dir (strict privacy), read-only
``/work``             the only writable path (``TMPDIR=/work/tmp``)
``/home/s``           a private, empty home
====================  ===================================================
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

#: The binary's own flags. ``--tools`` holds on its own, whatever the config
#: dir says: the process never has ``get_document`` or ``run_sql``, which do
#: emit names, formulas and SQL text.
DREAMSHEETS_ARGS: tuple[str, ...] = (
    "mcp-headless",
    "--read-only",
    "--config-dir",
    "/config",
    "--tools",
    "open_document,get_structure_summary",
)

SANDBOX_DOCUMENT_PATH = "/in/doc.dsheet"


@dataclass(frozen=True)
class RunLayout:
    """One run's private directory on the host."""

    root: Path

    @property
    def input_file(self) -> Path:
        return self.root / "in" / "doc.dsheet"

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def work_dir(self) -> Path:
        return self.root / "work"

    @property
    def tmp_dir(self) -> Path:
        return self.root / "work" / "tmp"

    @property
    def home_dir(self) -> Path:
        return self.root / "home"

    def create(self) -> None:
        for d in (self.input_file.parent, self.config_dir, self.tmp_dir, self.home_dir):
            d.mkdir(parents=True, exist_ok=True)


class ProtocolSandboxLauncher(Protocol):
    """Builds the command that runs the binary for one run."""

    @property
    def binary(self) -> Path:
        """The binary on the host, for hash pinning."""
        ...

    def document_path(self, layout: RunLayout) -> str:
        """The input's path as the binary sees it."""
        ...

    def command(self, layout: RunLayout) -> list[str]: ...

    def environment(self, layout: RunLayout) -> dict[str, str]:
        """The COMPLETE environment of the spawned process (nothing is inherited)."""
        ...


@dataclass(frozen=True)
class SystemLayout:
    """How the host lays out ``/lib``, ``/lib64`` and ``/bin``: a merged-usr
    host has symlinks into ``/usr``, an older one has real directories."""

    symlinks: dict[str, str]
    directories: tuple[str, ...]

    @classmethod
    def probe(cls) -> SystemLayout:
        links: dict[str, str] = {}
        dirs: list[str] = []
        for p in ("/lib", "/lib64", "/bin", "/sbin"):
            if os.path.islink(p):
                links[p] = os.readlink(p)
            elif os.path.isdir(p):
                dirs.append(p)
        return cls(links, tuple(dirs))


@dataclass(frozen=True)
class BwrapDreamsheetsLauncher:
    """Runs the binary from an extracted AppImage (``squashfs-root``) under
    bubblewrap. Never run the ``.AppImage`` itself: it needs FUSE.

    ``appimage_root`` is ``<dir>/squashfs-root`` from
    ``Dreamsheets.AppImage --appimage-extract``, done once, outside the sandbox.
    """

    appimage_root: Path
    bwrap: str = "/usr/bin/bwrap"
    system: SystemLayout | None = None

    @property
    def binary(self) -> Path:
        return self.appimage_root / "usr" / "bin" / "dreamsheets-desktop"

    def document_path(self, layout: RunLayout) -> str:
        return SANDBOX_DOCUMENT_PATH

    def command(self, layout: RunLayout) -> list[str]:
        return self.command_for(
            layout, ["/opt/ds/usr/bin/dreamsheets-desktop", *DREAMSHEETS_ARGS]
        )

    def command_for(self, layout: RunLayout, program: Sequence[str]) -> list[str]:
        """The same profile around any program: the sandbox probes run in it."""
        system = self.system or SystemLayout.probe()
        argv = [
            self.bwrap,
            # Every namespace (net, pid, ipc, uts, user, cgroup): no network,
            # loopback included, and no view of host processes or sockets.
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
            "--clearenv",
            "--ro-bind",
            "/usr",
            "/usr",
        ]
        for link, target in sorted(system.symlinks.items()):
            argv += ["--symlink", target, link]
        for d in system.directories:
            argv += ["--ro-bind", d, d]
        argv += [
            "--ro-bind-try",
            "/etc/ld.so.cache",
            "/etc/ld.so.cache",
            "--ro-bind",
            str(self.appimage_root),
            "/opt/ds",
            "--ro-bind",
            str(layout.input_file),
            SANDBOX_DOCUMENT_PATH,
            "--ro-bind",
            str(layout.config_dir),
            "/config",
            "--bind",
            str(layout.work_dir),
            "/work",
            "--bind",
            str(layout.home_dir),
            "/home/s",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--chdir",
            "/work",
            # bwrap's own root is a tmpfs: make it read-only once everything is
            # mounted, so /work and /home/s stay the only writable paths.
            "--remount-ro",
            "/",
        ]
        for key, value in sorted(self._inside_env().items()):
            argv += ["--setenv", key, value]
        argv += list(program)
        return argv

    def environment(self, layout: RunLayout) -> dict[str, str]:
        # bwrap itself gets almost nothing; --clearenv + --setenv set the
        # binary's environment explicitly.
        return {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}

    @staticmethod
    def _inside_env() -> dict[str, str]:
        return {
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/s",
            "TMPDIR": "/work/tmp",
            "LANG": "C.UTF-8",
            # The AppImage bundles webkit/gtk; the binary links them but the
            # headless path never initializes them. Prefer the bundled copies.
            "LD_LIBRARY_PATH": "/opt/ds/usr/lib",
        }


__all__ = [
    "DREAMSHEETS_ARGS",
    "SANDBOX_DOCUMENT_PATH",
    "BwrapDreamsheetsLauncher",
    "ProtocolSandboxLauncher",
    "RunLayout",
    "SystemLayout",
]
