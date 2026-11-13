# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeCacheTreeRetention — retires whole rebuildable cache trees (OMN-19998).

A cache tree entry names a directory whose direct children are rebuildable
units (Python virtual environments of the dod-verify check). A unit is
removed whole with ``shutil.rmtree`` — symlinks inside it included and never
followed — only when every regular file inside it is older than the entry's
``keep_days``. The owner rebuilds a unit from a lockfile on next use; the
unit is never archived.

Never touched: a unit that is a symlink; a unit whose own name is ``memory``,
``MEMORY.md``, ``.git`` or starts with the ledger's file-name prefix; a unit holding
a ``.git`` entry anywhere (git worktrees belong to lane closeout). Inside a
unit only ``MEMORY.md`` and names with the ledger's file-name prefix
are protected — a Python package directory named ``memory`` is a library, not
agent memory. A unit holding a protected name makes the entry report status
``error`` and removes nothing of that unit, stopping the entry. Before
removing a unit it is measured again (file count, total bytes, newest mtime)
and a difference from the first measure reports ``error`` and stops.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import stat
from pathlib import Path
from typing import Literal

import yaml

from omnimarket.nodes.node_retention_cleanup.handlers.handler_file_retention import (
    _PROTECTED_NAMES,
    _PROTECTED_PREFIX,
)
from omnimarket.nodes.node_retention_cleanup.models.model_cache_tree_retention import (
    CacheTreeEntry,
    CacheTreeItemResult,
    CacheTreePolicy,
    CacheTreeRequest,
    CacheTreeResult,
)

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"

# Names protected inside a unit. A Python package directory named ``memory`` is a library, not
# agent memory, so only the memory index and the ledger's file-name prefix are protected there.
_PROTECTED_INNER_NAMES: frozenset[str] = frozenset({"MEMORY.md"})


def load_cache_tree_policy(
    contract_path: Path = _CONTRACT_PATH,
) -> CacheTreePolicy:
    """Read ``retention_policy.cache_tree_retention`` from the node contract.

    Raises RuntimeError when the contract or the block is absent — retention
    must always be contract-driven, like ``load_file_policy``.
    """
    if not contract_path.exists():
        msg = f"contract.yaml not found at {contract_path}"
        raise RuntimeError(msg)
    raw = yaml.safe_load(contract_path.read_text()) or {}
    policy = (raw.get("retention_policy") or {}).get("cache_tree_retention")
    if policy is None:
        msg = (
            f"contract.yaml at {contract_path} does not declare a "
            "retention_policy.cache_tree_retention block"
        )
        raise RuntimeError(msg)
    return CacheTreePolicy.model_validate(policy)


def _resolve(path: str) -> Path | None:
    """Expand ``~`` and ``$VARS``; None when an unexpanded ``$`` remains."""
    expanded = os.path.expandvars(os.path.expanduser(path))
    if "$" in expanded:
        return None
    return Path(expanded)


class _Scan:
    """One walk of a unit: regular-file count, bytes, newest mtime, a protected inner name, a git entry."""

    __slots__ = ("bytes", "count", "git", "newest", "protected")

    def __init__(self) -> None:
        self.count = 0
        self.bytes = 0
        self.newest = 0.0
        self.protected = ""
        self.git = False

    def key(self) -> tuple[int, int, float]:
        return self.count, self.bytes, self.newest


def _scan(unit: Path) -> _Scan:
    out = _Scan()
    for dirpath, dirnames, filenames in os.walk(unit, followlinks=False):
        for name in dirnames + filenames:
            if name == ".git":
                out.git = True
            elif not out.protected and (
                name in _PROTECTED_INNER_NAMES or name.startswith(_PROTECTED_PREFIX)
            ):
                out.protected = name
        for name in filenames:
            info = os.lstat(os.path.join(dirpath, name))
            if stat.S_ISREG(info.st_mode):
                out.count += 1
                out.bytes += info.st_size
                out.newest = max(out.newest, info.st_mtime)
    return out


def _is_protected_unit(name: str) -> bool:
    return (
        name in _PROTECTED_NAMES or name == ".git" or name.startswith(_PROTECTED_PREFIX)
    )


class NodeCacheTreeRetention:
    """Retires aged rebuildable cache trees per the contract's entries."""

    def handle(self, request: CacheTreeRequest) -> CacheTreeResult:
        items = [
            self._run_entry(entry, request)
            for entry in request.entries
            if not request.only or entry.name in request.only
        ]
        return CacheTreeResult(
            items=items,
            units_removed=sum(i.units_removed for i in items),
            files_removed=sum(i.files_removed for i in items),
            bytes_removed=sum(i.bytes_removed for i in items),
        )

    def _run_entry(
        self, entry: CacheTreeEntry, request: CacheTreeRequest
    ) -> CacheTreeItemResult:
        if not entry.enabled:
            return CacheTreeItemResult(name=entry.name, status="disabled")
        root = _resolve(entry.path)
        if root is None or not root.is_dir() or root.is_symlink():
            return CacheTreeItemResult(name=entry.name, status="absent")
        cutoff = (request.now - dt.timedelta(days=entry.keep_days)).timestamp()
        scans: list[tuple[Path, _Scan]] = [
            (child, _scan(child))
            for child in sorted(root.glob(entry.pattern), key=lambda p: p.name)
            if child.is_dir()
            and not child.is_symlink()
            and not _is_protected_unit(child.name)
        ]
        before = sum(scan.bytes for _child, scan in scans)
        counts = {"eligible": 0, "ebytes": 0, "removed": 0, "files": 0, "bytes": 0}
        error = ""
        for child, scan in scans:
            if scan.git:
                continue  # a git worktree: lane closeout owns it
            if scan.protected:
                error = f"{child.name}: whole tree holds a protected name"
                break
            if scan.newest >= cutoff:
                continue
            counts["eligible"] += 1
            counts["ebytes"] += scan.bytes
            if request.dry_run:
                continue
            if _scan(child).key() != scan.key():
                error = f"{child.name}: unit changed before removal"
                break
            try:
                shutil.rmtree(child)
            except OSError as exc:
                error = f"{child.name}: {type(exc).__name__}"
                break
            counts["removed"] += 1
            counts["files"] += scan.count
            counts["bytes"] += scan.bytes
        after = before - counts["bytes"]
        status: Literal["ok", "dry_run", "error"] = (
            "error" if error else "dry_run" if request.dry_run else "ok"
        )
        return CacheTreeItemResult(
            name=entry.name,
            status=status,
            units_eligible=counts["eligible"],
            units_removed=counts["removed"],
            files_removed=counts["files"],
            bytes_eligible=counts["ebytes"],
            bytes_removed=counts["bytes"],
            bytes_before=before,
            bytes_after=after,
            error=error,
        )
