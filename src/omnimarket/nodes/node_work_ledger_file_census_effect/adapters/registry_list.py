# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The registry's clone list as the census's repositories, and the seed of ratchet rows for a repository with none.

The clone list is the registry root's ``repos.yaml``: ``repositories`` is a list of ``{name, repo, ...}``. It is read the
way the other registry-wide nodes read it (the patch-apply dispatch and the PRRP snapshot publish): every ``name`` is a
registered repository, none is dropped, and a list that cannot be read or names no repository is an error, never an empty
census.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import yaml

from omnimarket.nodes.node_work_ledger_file_census_effect.models.model_file_census import (
    ModelCensusRepository,
)


def read_registry_repositories(path: Path) -> tuple[ModelCensusRepository, ...]:
    """Every ``name`` of the registry's clone list, in list order, each counted in the total."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    rows = document.get("repositories") if isinstance(document, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{path} lists no repositories")
    names: list[str] = []
    for row in rows:
        name = row.get("name") if isinstance(row, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{path} has a repository row with no name")
        if name not in names:
            names.append(name)
    return tuple(ModelCensusRepository(name=name, own_line=False) for name in names)


def read_registry_clone_aliases(path: Path) -> dict[str, str]:
    """``{name: last segment of repo}`` where the two differ (a clone kept under its repository's spelling)."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    aliases: dict[str, str] = {}
    for row in document.get("repositories", []) if isinstance(document, dict) else []:
        if not isinstance(row, dict):
            continue
        name, repo = row.get("name"), row.get("repo")
        if isinstance(name, str) and isinstance(repo, str):
            segment = repo.rsplit("/", 1)[-1]
            if segment and segment != name:
                aliases[name] = segment
    return aliases


def resolve_directories(
    root: Path,
    aliases: Mapping[str, str],
    explicit: Mapping[str, str],
) -> dict[str, str]:
    """Where each repository sits: an explicit directory wins; else the alias when only the alias is a checkout.

    A name with no checkout under either spelling is left alone, so the census reports it absent.
    """
    resolved = dict(explicit)
    for name, segment in aliases.items():
        if name in resolved:
            continue
        if not (root / name / ".git").exists() and (root / segment / ".git").exists():
            resolved[name] = segment
    return resolved


def seed_missing_rows(
    path: Path,
    ratchet_rows: Mapping[str, int],
    file_rows: Mapping[str, Mapping[str, int]],
    header: str,
) -> tuple[str, ...]:
    """Add the ratchet row and the per-file table of each repository the file does not hold; return the repositories added.

    A row already on disk is never changed, raised or lowered: this only adds a repository the file lacks, from a census
    run's own counts. The file is replaced whole, so a write that dies midway leaves the earlier file in place.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("ratchet file is not a mapping")
    ratchet = dict(data.get("ratchet") or {})
    files = {repo: dict(rows) for repo, rows in (data.get("files") or {}).items()}
    added: list[str] = []
    for repository, count in ratchet_rows.items():
        if repository in ratchet and repository in files:
            continue
        ratchet.setdefault(repository, count)
        files.setdefault(repository, dict(file_rows.get(repository, {})))
        added.append(repository)
    if not added:
        return ()
    body = yaml.safe_dump(
        {
            "files": dict(sorted(files.items())),
            "ratchet": dict(sorted(ratchet.items())),
        },
        sort_keys=True,
    )
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(header + body, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return tuple(added)
