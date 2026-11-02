# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Staged artifacts of a blocked ruled-work run that sit on this host."""

from __future__ import annotations

from pathlib import Path


def local_artifacts(rows: list[dict[str, str]], run_root: Path) -> None:
    """Retain actual staged patch locations when the blocked run is on this host.

    Remote run ids and their host remain in the brief when their files cannot be
    read here. A path is filled only from an existing run, never invented.
    """
    for row in rows:
        run = row.get("run", "")
        if row["action"] != "CLAIM" or not run or "/" in run or run in {".", ".."}:
            continue
        directory = run_root / run
        if not directory.is_dir():
            continue
        row.setdefault("run-directory", str(directory))
        context = run_root / f"{run}.ctx"
        try:
            repository = (context / "repository").read_text(encoding="utf-8").strip()
            if repository and all(c.isalnum() or c in "_/.-" for c in repository):
                row.setdefault("repo", repository.split("/")[-1])
        except (OSError, UnicodeError):
            pass
        patches = [
            context / "landing-artifact/change.patch",
            *sorted((context / "scratch").glob("*staged*.patch")),
        ]
        existing = [str(p) for p in patches if p.is_file()]
        if existing:
            row.setdefault("patch-path", ", ".join(existing))
