# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Retirement input inspection for the host reconcile effect.

The reconciler calls this before invoking any clone delegate. The same inspection
is exercised over fixture registries by the CI and pre-commit falsifier.
"""

from __future__ import annotations

import ast
import json
import os
import re
from collections.abc import Iterator
from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import find_node_overlay
from omnimarket.models.model_repository_retirement import (
    ModelRepositoryRetirementOverlay,
    ModelRepositoryRetirementPolicy,
    ModelRetiredRepositoryFinding,
)


def load_retirement_policy() -> ModelRepositoryRetirementPolicy:
    contract = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "contract.yaml").read_text()
    )
    policy = ModelRepositoryRetirementPolicy.model_validate(contract["retirement"])
    path = find_node_overlay("node_host_reconcile_effect")
    if path is None:
        return policy
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("host reconcile overlay must be a mapping")
    overlay = ModelRepositoryRetirementOverlay.model_validate(
        document.get("retirement", {})
    )
    return policy.model_copy(
        update={
            "retired_repositories": tuple(
                dict.fromkeys(
                    (
                        *policy.retired_repositories,
                        *overlay.additional_retired_repositories,
                    )
                )
            )
        }
    )


def retired_repository_findings(
    root: Path, policy: ModelRepositoryRetirementPolicy
) -> tuple[ModelRetiredRepositoryFinding, ...]:
    """Inspect one repository's scripts and nested configuration/source lists.

    No sibling checkout is required. A host passes its registry and each present
    active checkout separately; CI passes its own checkout and fixture trees.
    Comments and historical prose are not repository targets.
    """
    retired = set(policy.retired_repositories)
    token = re.compile(
        r"(?<![A-Za-z0-9_.-])("
        + "|".join(re.escape(name) for name in sorted(retired))
        + r")(?:\.git)?(?![A-Za-z0-9_.-])"
    )
    key_pattern = re.compile(policy.registry_key_pattern)
    constant_pattern = re.compile(policy.registry_constant_pattern)
    findings: set[tuple[str, str, str]] = set()

    def note(path: Path, names: set[str], kind: str) -> None:
        findings.update((str(path.relative_to(root)), name, kind) for name in names)

    def scalars(value: object) -> set[str]:
        if isinstance(value, str):
            return {value.rsplit("/", 1)[-1].removesuffix(".git")} & retired
        if isinstance(value, dict):
            return set().union(*(scalars(v) for v in value.values()))
        if isinstance(value, (list, tuple)):
            return set().union(*(scalars(v) for v in value))
        return set()

    def registry_values(value: object) -> set[str]:
        if isinstance(value, dict):
            names: set[str] = set()
            for key, nested in value.items():
                if isinstance(key, str) and key_pattern.fullmatch(key):
                    names.update(scalars(nested))
                names.update(registry_values(nested))
            return names
        if isinstance(value, list):
            return set().union(*(registry_values(v) for v in value))
        return set()

    def files_under(directory: Path) -> Iterator[Path]:
        if directory.is_symlink():
            raise ValueError(f"{directory}: symbolic registry input is refused")
        markers = frozenset(policy.generated_directory_markers)
        for current, dirnames, filenames in os.walk(directory, followlinks=False):
            if markers.intersection(filenames):
                # A generated environment is not declared input; its own
                # layout (lib64 -> lib) is never a sync or registry source.
                dirnames.clear()
                continue
            for name in (*dirnames, *filenames):
                path = Path(current, name)
                if path.is_symlink():
                    raise ValueError(f"{path}: symbolic registry input is refused")
            for name in filenames:
                yield Path(current, name)

    scripts = {
        path
        for directory in policy.sync_directories
        for path in files_under(root / directory)
    }
    source_files = {path for path in files_under(root / "src") if path.suffix == ".py"}
    sync_sources = {
        path for path in source_files if path.match(policy.source_sync_glob)
    }
    for path in sorted(scripts | sync_sources):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if path.suffix == ".py":
            # Generic Python scripts are checked as structured registry lists
            # below. Only sync sources treat all literal operands as targets.
            if path not in sync_sources and "sync" not in path.stem:
                continue
            tree = ast.parse(text, filename=str(path))
            for block in ast.walk(tree):
                if (
                    isinstance(
                        block,
                        (
                            ast.Module,
                            ast.ClassDef,
                            ast.FunctionDef,
                            ast.AsyncFunctionDef,
                        ),
                    )
                    and block.body
                    and isinstance(block.body[0], ast.Expr)
                ):
                    expr = block.body[0].value
                    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
                        block.body.pop(0)
            names = {
                name
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                for name in token.findall(node.value)
            }
        else:
            names = {
                name
                for line in text.splitlines()
                if not line.lstrip().startswith(("#", ";", "//"))
                for name in token.findall(line)
            }
        note(path, names, "sync script")

    configs = {
        path for pattern in policy.registry_file_globs for path in root.glob(pattern)
    }
    for path in sorted(configs):
        if path.is_symlink():
            raise ValueError(f"{path}: symbolic registry input is refused")
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        try:
            value = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
        except (json.JSONDecodeError, yaml.YAMLError):
            raise ValueError(f"{path}: malformed registry configuration") from None
        note(path, registry_values(value), "registry list")

    sources = source_files | {path for path in scripts if path.suffix == ".py"}
    for path in sorted(sources):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for statement in ast.walk(tree):
            if isinstance(statement, ast.Assign):
                targets = statement.targets
                value = statement.value
            elif isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
                targets = [statement.target]
                value = statement.value
            elif (
                isinstance(statement, ast.Call)
                and isinstance(statement.func, ast.Attribute)
                and statement.func.attr in {"append", "extend", "add", "update"}
            ):
                targets = [statement.func.value]
                value = statement
            else:
                continue
            if any(
                isinstance(target, ast.Name) and constant_pattern.fullmatch(target.id)
                for target in targets
            ):
                strings = (
                    {
                        node.value.rsplit("/", 1)[-1].removesuffix(".git")
                        for node in ast.walk(value)
                        if isinstance(node, ast.Constant)
                        and isinstance(node.value, str)
                    }
                    if value is not None
                    else set()
                )
                note(path, strings & retired, "registry list")

    return tuple(
        ModelRetiredRepositoryFinding(path=path, repository=name, kind=kind)
        for path, name, kind in sorted(findings)
    )
