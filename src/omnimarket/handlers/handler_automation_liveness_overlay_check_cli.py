# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command line of the liveness overlay completeness check (OMN-20804).

``onex-automation-liveness-check`` reads the overlay, the sources declaration and every file of
every registry repository the declaration names, hands the text to the pure handler, and prints
what it found. Exit 0 means the overlay and the installers agree, 1 means findings, 2 means the
check could not run (a repository is absent and ``--internal-only`` was not asked for).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import yaml

from omnimarket.nodes.node_automation_liveness_overlay_check_compute.handlers.handler_automation_liveness_installer_scan import (
    EXCLUDED_DIRECTORIES,
    is_candidate_path,
)
from omnimarket.nodes.node_automation_liveness_overlay_check_compute.handlers.handler_automation_liveness_overlay_check import (
    HandlerAutomationLivenessOverlayCheck,
)
from omnimarket.nodes.node_automation_liveness_overlay_check_compute.models import (
    ModelAutomationLivenessOverlayCheckRequest,
    ModelAutomationLivenessScannedFile,
)

#: The overlay and its sources declaration live in the private repository, under
#: ``src/<package>/overlays``; the package directory is found, never named here.
OVERLAY_FILENAME = "automation_liveness_overlay.yaml"
SOURCES_FILENAME = "automation_liveness_sources.yaml"
_MAX_FILE_BYTES = 1_000_000


def _read_text(path: Path) -> str:
    """The console boundary reads each file; the handlers only ever see text."""
    return path.read_text(encoding="utf-8")


def find_private_file(root: Path, filename: str) -> Path:
    """The ``src/*/overlays/<filename>`` file of the private checkout at ``root``."""
    matches = sorted(root.glob(f"src/*/overlays/{filename}"))
    return matches[0] if matches else root / "src" / "overlays" / filename


def _environment_value(name: str) -> str | None:
    return os.environ.get(name)


def collect_files(
    repository: str, root: Path
) -> tuple[ModelAutomationLivenessScannedFile, ...]:
    """Every readable candidate file under ``root``, with paths relative to it."""
    files: list[ModelAutomationLivenessScannedFile] = []
    for directory, subdirectories, names in os.walk(root):
        # A nested git repository or worktree is another repository's files, not this one's.
        subdirectories[:] = sorted(
            d
            for d in subdirectories
            if d not in EXCLUDED_DIRECTORIES
            and not d.startswith(".")
            and not Path(directory, d, ".git").exists()
        )
        for name in sorted(names):
            path = Path(directory, name)
            relative = path.relative_to(root).as_posix()
            if not is_candidate_path(relative) or path.is_symlink():
                continue
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    continue
                text = _read_text(path)
            except (OSError, UnicodeDecodeError):
                continue
            files.append(
                ModelAutomationLivenessScannedFile(
                    repository=repository, path=relative, text=text
                )
            )
    return tuple(files)


def _declared_repositories(sources_text: str) -> tuple[str, ...]:
    try:
        declared = yaml.safe_load(sources_text)
    except yaml.YAMLError:
        return ()
    names = declared.get("repositories") if isinstance(declared, dict) else None
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        return ()
    return tuple(names)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="onex-automation-liveness-check", description=__doc__
    )
    parser.add_argument(
        "--internal-root",
        type=Path,
        default=Path.cwd(),
        help="the private checkout holding the overlay (default: the current directory)",
    )
    parser.add_argument(
        "--registry-root",
        type=Path,
        default=None,
        help="the directory holding the other registry clones (default: $OMNI_HOME)",
    )
    parser.add_argument(
        "--private-repository",
        default=None,
        help="the name the sources declaration gives the private checkout "
        "(default: the directory name of --internal-root)",
    )
    parser.add_argument(
        "--internal-only",
        action="store_true",
        help="scan only this checkout; the result says it is partial",
    )
    parser.add_argument(
        "--overlay", type=Path, default=None, help="overlay file to check"
    )
    parser.add_argument(
        "--sources", type=Path, default=None, help="sources declaration to check"
    )
    args = parser.parse_args(argv)

    internal: Path = args.internal_root
    overlay_path: Path = args.overlay or find_private_file(internal, OVERLAY_FILENAME)
    sources_path: Path = args.sources or find_private_file(internal, SOURCES_FILENAME)
    for required in (overlay_path, sources_path):
        if not required.is_file():
            sys.stderr.write(f"onex-automation-liveness-check: {required} is missing\n")
            return 2
    sources_text = _read_text(sources_path)
    repositories = _declared_repositories(sources_text)
    private_repository: str = args.private_repository or internal.resolve().name

    registry_value = args.registry_root or _environment_value("OMNI_HOME")
    registry_root = Path(registry_value) if registry_value else None
    files: list[ModelAutomationLivenessScannedFile] = []
    scanned: list[str] = []
    for repository in repositories:
        if repository == private_repository:
            root: Path | None = internal
        elif args.internal_only or registry_root is None:
            root = None
        else:
            root = registry_root / repository
        if root is not None and root.is_dir():
            scanned.append(repository)
            files.extend(collect_files(repository, root))
    if not args.internal_only and registry_root is None:
        sys.stderr.write(
            "onex-automation-liveness-check: set OMNI_HOME or pass --registry-root, "
            "or pass --internal-only for a partial run\n"
        )
        return 2

    result = HandlerAutomationLivenessOverlayCheck().handle(
        ModelAutomationLivenessOverlayCheckRequest(
            overlay_text=_read_text(overlay_path),
            sources_text=sources_text,
            scanned_repositories=tuple(scanned),
            files=tuple(files),
            complete=not args.internal_only,
        )
    )
    for finding in result.findings:
        sys.stdout.write(f"{finding.render()}\n")
    scope = "complete" if result.complete else "PARTIAL (internal only)"
    sys.stdout.write(
        f"automation-liveness overlay: {result.entries_checked} entries, "
        f"{result.sources_found} sources found in {len(result.repositories_scanned)} "
        f"repositories, {len(result.findings)} findings, scope {scope}\n"
    )
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
