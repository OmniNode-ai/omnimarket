# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real backend of the replay's tree: a frozen snapshot of a checkout, as a file and as a directory.

``build_tree_snapshot`` reduces a checkout to what the selector reads of it. ``materialize_tree`` writes
that reduction back out as a directory of stub files whose imports, spelled node names and directory
layout are the checkout's, so ``HandlerTestPathResolution`` runs over it unchanged.
"""

from __future__ import annotations

import gzip
import hashlib
from pathlib import Path

import yaml

from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_durations_file import (
    SourceDurationsFile,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_text_file import (
    SourceTextFile,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers import (
    handler_test_path_resolution as resolution,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_selection_replay import (
    ModelSnapshotFile,
    ModelTreeSnapshot,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_path_resolution import (
    ModelAdjacencyMap,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_selector import (
    ModelSelectorPolicy,
)

_SOURCE = SourceTextFile()


def _python_files(repo_root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for base in (repo_root / "src" / resolution.PACKAGE, repo_root / "tests"):
        for path in base.rglob("*.py"):
            files[path.relative_to(repo_root).as_posix()] = path
    return files


def build_tree_snapshot(
    repo_root: Path,
    tree_id: str,
    adjacency: ModelAdjacencyMap,
    policy: ModelSelectorPolicy,
) -> ModelTreeSnapshot:
    """Reduce the checkout at ``repo_root`` (the tree ``tree_id``) to a snapshot."""
    node_scoped = frozenset(adjacency.node_scoped_modules)
    sources = _python_files(repo_root)
    dotted = {rel: resolution.dotted_module(rel) for rel in sources}
    packages = {d for rel, d in dotted.items() if rel.endswith("/__init__.py")}
    lookup = resolution.ImportGraph(frozenset(dotted.values()), {}, {}, {}, {})
    files: dict[str, ModelSnapshotFile] = {}
    for rel, path in sources.items():
        text = _SOURCE.read_text(path)
        vertex = resolution.graph_vertex(dotted[rel], node_scoped)
        targets: set[str] = set()
        for name in resolution.module_references(
            text, dotted[rel], dotted[rel] in packages
        ):
            target = lookup.resolve(name)
            if (
                target is not None
                and resolution.graph_vertex(target, node_scoped) != vertex
            ):
                targets.add(target)
        names: set[str] = set()
        relative: set[str] = set()
        if rel.startswith(resolution.TEST_PREFIX):
            names = set(resolution.NODE_NAME.findall(text))
            # A target spelling a node name the text does not (a relative import) is written back as one.
            relative = {
                t for t in targets if not set(resolution.NODE_NAME.findall(t)) <= names
            }
        files[rel] = ModelSnapshotFile(
            refs=tuple(sorted(targets - relative)),
            relative_refs=tuple(sorted(relative)),
            names=tuple(sorted(names)),
        )

    dirs = sorted(
        path.relative_to(repo_root).as_posix()
        for base in (repo_root / "src" / resolution.PACKAGE, repo_root / "tests")
        for path in [base, *base.rglob("*")]
        if path.is_dir()
    )
    yaml_names: dict[str, tuple[str, ...]] = {}
    for module in sorted(node_scoped):
        module_root = repo_root / "src" / resolution.PACKAGE / module
        if not module_root.is_dir():
            continue
        for node in sorted(module_root.iterdir()):
            if not (node.is_dir() and node.name.startswith(resolution.NODE_PREFIX)):
                continue
            for path in sorted(node.rglob("*.y*ml")):
                found = sorted(
                    set(resolution.NODE_NAME.findall(_SOURCE.read_text(path)))
                )
                if found:
                    yaml_names[path.relative_to(repo_root).as_posix()] = tuple(found)

    durations = SourceDurationsFile().read(str(repo_root), policy.durations_path)
    if durations is None:
        raise ValueError(f"unreadable durations file {policy.durations_path}")
    return ModelTreeSnapshot(
        tree_id=tree_id,
        adjacency=adjacency,
        policy=policy,
        durations=dict(sorted(durations.items())),
        dirs=tuple(dirs),
        files=dict(sorted(files.items())),
        yaml_names=dict(sorted(yaml_names.items())),
    )


def _package_of(rel: str) -> list[str]:
    parts = resolution.dotted_module(rel).split(".")
    return parts if rel.endswith("/__init__.py") else parts[:-1]


def _relative_import(package: list[str], target: str) -> str:
    """``from <dots><rest> import _`` that a file of ``package`` resolves to ``target``."""
    wanted = target.split(".")
    common = 0
    while (
        common < len(package)
        and common < len(wanted)
        and package[common] == wanted[common]
    ):
        common += 1
    return (
        f"from {'.' * (len(package) - common + 1)}{'.'.join(wanted[common:])} import _"
    )


def materialize_tree(snapshot: ModelTreeSnapshot, root: Path) -> None:
    """Write the snapshot's stub tree under ``root`` (an existing, empty directory)."""
    for rel in snapshot.dirs:
        (root / rel).mkdir(parents=True, exist_ok=True)
    for rel, entry in snapshot.files.items():
        lines = [f"import {target}" for target in entry.refs]
        package = _package_of(rel)
        lines += [_relative_import(package, target) for target in entry.relative_refs]
        if entry.names:
            lines.append("# " + " ".join(entry.names))
        stub = root / rel
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for rel, names in snapshot.yaml_names.items():
        stub = root / rel
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text("# " + " ".join(names) + "\n", encoding="utf-8")


def snapshot_bytes(snapshot: ModelTreeSnapshot) -> bytes:
    """The snapshot file's bytes: compact JSON, gzip-compressed with no timestamp (deterministic)."""
    return gzip.compress(snapshot.model_dump_json().encode(), mtime=0)


def snapshot_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_snapshot_file(
    repo_root: str, snapshot_path: str, expected_sha256: str
) -> ModelTreeSnapshot:
    """The snapshot at ``snapshot_path``, refused when its bytes are not the ones named."""
    data = (Path(repo_root) / snapshot_path).read_bytes()
    actual = snapshot_sha256(data)
    if actual != expected_sha256:
        raise ValueError(
            f"snapshot {snapshot_path} has sha256 {actual}, not {expected_sha256}"
        )
    text = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
    return ModelTreeSnapshot.model_validate_json(text)


def load_adjacency(path: Path) -> ModelAdjacencyMap:
    return ModelAdjacencyMap.model_validate(yaml.safe_load(_SOURCE.read_text(path)))
