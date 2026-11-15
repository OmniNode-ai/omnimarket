# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The size cap pass for a uv cache: evict whole entries oldest-first until the measured total is at or under cap_gib."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from omnimarket.nodes.node_host_retention_effect.handlers.host_retention_pass import (
    _Pass,
)
from omnimarket.nodes.node_host_retention_effect.handlers.host_retention_ports import (
    HostRetentionPorts,
)
from omnimarket.nodes.node_host_retention_effect.models.model_host_retention import (
    ModelRetentionItem,
    ModelUvArchiveEntry,
    ModelUvBucketBound,
    ModelUvCache,
)
from omnimarket.nodes.node_temp_retention_effect.handlers.handler_temp_retention import (
    ProcessReferences,
    _held_names,
)

# The declared class name of the uv cache (the handler owns it as UV_CACHE; importing it here would be circular).
CLASS_NAME = "uv_cache"

_GIB = 2**30
_DAY = 86400.0


def _is_held(
    bucket_dir: str,
    name: str,
    refs: ProcessReferences,
    seen: dict[str, set[str]],
) -> bool:
    """A live process names the entry: its working directory or a command, environment or open file holds it."""
    *parents, leaf = name.split("/")
    root = str(Path(bucket_dir, *parents))
    if root not in seen:
        seen[root] = _held_names(Path(root), refs)
    return leaf in seen[root]


def evict_over_cap(
    cache: ModelUvCache,
    *,
    size_after: int,
    now: float,
    dry_run: bool,
    buckets: dict[str, ModelUvBucketBound],
    refs: ProcessReferences | None,
    read_refs: Callable[[], ProcessReferences | None],
    ports: HostRetentionPorts,
    out: _Pass,
) -> None:
    """Evict whole entries of the cap's buckets oldest-first until the measured total is at or under cap_gib.

    ``buckets`` is the contract's evictable folders. An entry is kept (never removed) when, in a folder whose contract row says
    keep_linked, it is linked into a virtual environment, changed within keep_days, named by a live process, or a symlink or not a folder. A dry run lists the entries
    it would evict and removes nothing; there the running total is the size read before the prune, because the prune frees an
    unknown amount, and every entry is held against the references read at the start. A real run reads the references again
    right before each removal and holds the entry against that fresh reading, so a process that started mid-pass keeps its
    entry; a re-read that fails adds an error and stops the pass, nothing further is removed. A references failure at the
    start adds an error and evicts nothing; a bucket whose scan fails adds an error and is skipped, the others still run: the
    prune's result stands either way.
    """
    cap = cache.cap
    if cap is None:
        return
    target = cap.cap_gib * _GIB
    if size_after <= target:
        return
    if refs is None:
        out.errors.append(
            "process references unreadable; the uv cache cap pass is skipped"
        )
        return
    candidates: list[tuple[str, bool, str | None, ModelUvArchiveEntry]] = []
    for bucket in cap.buckets:
        bucket_dir = str(Path(cache.path) / bucket)
        bound = buckets[bucket]
        try:
            scanned = ports.uv_archive.scan(
                bucket_dir, bound.depth, cache.timeout_seconds, bound.marker_suffix
            )
        except OSError as exc:
            out.errors.append(f"{bucket_dir}: {type(exc).__name__}")
            continue
        candidates.extend(
            (bucket_dir, bound.keep_linked, bound.marker_suffix, entry)
            for entry in scanned
        )
    cutoff = now - cap.keep_days * _DAY
    seen: dict[str, set[str]] = {}
    total = size_after
    for bucket_dir, keep_linked, marker_suffix, entry in sorted(
        candidates, key=lambda c: (c[3].newest_mtime, c[0], c[3].name)
    ):
        if total <= target:
            return
        path = f"{bucket_dir}/{entry.name}"
        reason: str | None = None
        if entry.special:
            reason = "special_file"
        elif keep_linked and entry.linked:
            reason = "linked_by_venv"
        elif entry.newest_mtime >= cutoff:
            reason = "too_new"
        else:
            if not dry_run:
                # The references are read again before each removal: a process may have started
                # since the pass read them once at the start.
                fresh = read_refs()
                if fresh is None:
                    out.errors.append(
                        "process references unreadable; the uv cache cap pass stops"
                    )
                    return
                refs = fresh
                seen.clear()
            if _is_held(bucket_dir, entry.name, refs, seen):
                reason = "held_by_process"
        if reason is not None:
            out.keep(CLASS_NAME, path, reason)
            continue
        out.propose(
            ModelRetentionItem(
                class_name=CLASS_NAME,
                path=path,
                bytes=entry.bytes,
                action="evict",
                reason=(
                    "dry run: the running total is the size read before the prune; "
                    "the prune frees an unknown amount"
                    if dry_run
                    else f"uv cache at {size_after} bytes is over its cap_gib {cap.cap_gib}"
                ),
            )
        )
        if not dry_run:
            try:
                ports.uv_archive.remove(bucket_dir, entry.name, marker_suffix)
            except OSError as exc:
                out.errors.append(f"{path}: {type(exc).__name__}")
                continue
            out.done(CLASS_NAME, entry.bytes)
        total -= entry.bytes
