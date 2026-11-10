# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every per-repository key of the compare history under the registry's canonical repository name (OMN-23134, AC1).

The history is recorded as each source spelled the repository, and stays so. The read maps each key whose family the overlay
names to the canonical name. Two spellings of one repository in one snapshot are two reads of the same repository, so the
snapshot keeps the larger reading (a sum would count its merges twice) and the family's total drops the repeat. A spelling
the overlay does not map stays a row of its own, and ``unmapped_spellings`` names the pairs that differ only by ``-`` and ``_``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from omnimarket.nodes.node_floor_compare_effect.models.model_floor_compare import (
    ModelSnapshot,
)
from omnimarket.nodes.node_floor_compare_effect.models.model_repository_names import (
    ModelRepositoryNameEntry,
    ModelRepositoryNames,
)

NODE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OVERLAY = NODE_DIR / "overlay.repository_names.yaml"
OVERLAY_ENV = "ONEX_FLOOR_REPOSITORY_NAMES_OVERLAY"


def overlay_path(environ: Mapping[str, str], home: Path, host_relpath: str) -> Path:
    """The overlay file: the one the env var names, else the host's own under the registry home, else the node's shipped one."""
    named = environ.get(OVERLAY_ENV, "").strip()
    if named:
        return Path(named)
    host = home / host_relpath
    return host if host.is_file() else DEFAULT_OVERLAY


def _registry_entries(
    names: ModelRepositoryNames, home: Path
) -> tuple[tuple[ModelRepositoryNameEntry, ...], tuple[str, ...]]:
    """Entries for the repositories the registry's clone list spells two ways; a spelling the overlay already owns is left to it."""
    if not names.registry_relpath:
        return (), ()
    path = (home / names.registry_relpath).resolve()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        listed = [(str(r["name"]), str(r["repo"])) for r in raw["repositories"]]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return (), (
            f"the registry clone list {names.registry_relpath} cannot be read ({type(exc).__name__}): "
            "only the overlay's own entries name a repository's other spellings",
        )
    overlay_names = {
        name for e in names.repositories for name in (e.canonical, *e.aliases)
    }
    registry_names = {name for name, _ in listed}
    taken = set(overlay_names)
    entries: list[ModelRepositoryNameEntry] = []
    for name, repo in listed:
        alias = repo.rsplit("/", 1)[-1]
        if (
            alias == name
            or alias in registry_names
            or alias in taken
            or name in overlay_names
        ):
            continue
        taken.add(alias)
        entries.append(
            ModelRepositoryNameEntry(
                canonical=name,
                aliases=(alias,),
                evidence=f"the registry clone list names the clone {name} and the repository {repo}",
            )
        )
    return tuple(entries), ()


def load_repository_names(
    environ: Mapping[str, str], home: Path, host_relpath: str
) -> tuple[ModelRepositoryNames, tuple[str, ...]]:
    """The overlay's repositories joined with the ones the registry's clone list spells two ways, and the notes of what could not be read."""
    raw = yaml.safe_load(
        overlay_path(environ, home, host_relpath).read_text(encoding="utf-8")
    )
    names = ModelRepositoryNames.model_validate(raw["repository_names"])
    derived, notes = _registry_entries(names, home)
    joined = names.model_copy(update={"repositories": (*names.repositories, *derived)})
    return ModelRepositoryNames.model_validate(joined.model_dump()), notes


def canonical_map(names: ModelRepositoryNames) -> dict[str, str]:
    """Each alias to its canonical name."""
    return {
        alias: entry.canonical
        for entry in names.repositories
        for alias in entry.aliases
    }


def canonical_snapshot(
    snapshot: ModelSnapshot, names: ModelRepositoryNames
) -> ModelSnapshot:
    """``snapshot`` with each per-repository key under its canonical name, one value per repository."""
    aliases = canonical_map(names)
    if not aliases:
        return snapshot
    values = dict(snapshot.values)
    labels = dict(snapshot.labels)
    for family in names.families:
        repeated = 0.0
        for alias, canonical in aliases.items():
            old, new = f"{family.prefix}{alias}", f"{family.prefix}{canonical}"
            if old not in values:
                continue
            value = values.pop(old)
            if new in values:
                # The same repository read twice: keep the larger reading and take the repeat out of the family total.
                repeated += min(value, values[new])
                values[new] = max(value, values[new])
            else:
                values[new] = value
            if old in labels:
                labels.setdefault(new, labels.pop(old))
        if family.total_key and family.total_key in values and repeated:
            values[family.total_key] = max(values[family.total_key] - repeated, 0.0)
    if values == snapshot.values and labels == snapshot.labels:
        return snapshot
    return snapshot.model_copy(update={"values": values, "labels": labels})


def canonical_snapshots(
    snapshots: tuple[ModelSnapshot, ...], names: ModelRepositoryNames
) -> tuple[ModelSnapshot, ...]:
    return tuple(canonical_snapshot(s, names) for s in snapshots)


def _fold(name: str) -> str:
    return name.replace("-", "_").lower()


def unmapped_spellings(
    snapshot: ModelSnapshot, names: ModelRepositoryNames
) -> tuple[str, ...]:
    """Notes for repositories that read as two rows because their spellings differ and no overlay entry joins them."""
    notes: list[str] = []
    for family in names.families:
        seen: dict[str, str] = {}
        for key in sorted(snapshot.values):
            if not key.startswith(family.prefix):
                continue
            repo = key[len(family.prefix) :]
            other = seen.setdefault(_fold(repo), repo)
            if other != repo:
                notes.append(
                    f"{family.prefix}{other} and {family.prefix}{repo} read as two repositories: add one entry to the repository names overlay"
                )
    return tuple(notes)
