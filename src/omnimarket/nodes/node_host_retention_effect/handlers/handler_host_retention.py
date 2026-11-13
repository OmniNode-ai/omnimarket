# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Age out lane scratch/results, evict runner work and prune uv caches on a host.

Every host, path and number the node acts on is a field of the overlay model. The contract declares
only the bounds an overlay may not exceed. A pass refuses before it reads or removes anything when a
declaration is unsafe; otherwise it runs the requested classes and reports what it removed, kept
and freed.
"""

from __future__ import annotations

import fnmatch
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_host_retention_effect.handlers.handler_uv_cache_cap import (
    evict_over_cap,
)
from omnimarket.nodes.node_host_retention_effect.handlers.host_retention_ports import (
    HostRetentionPorts,
    PreCommitStorePort,
    read_references_portable,
)
from omnimarket.nodes.node_host_retention_effect.models.model_host_retention import (
    ModelAgeClass,
    ModelClassTotals,
    ModelDiskReading,
    ModelHostRetention,
    ModelHostRetentionOverlay,
    ModelHostRetentionRequest,
    ModelHostRetentionResult,
    ModelRetentionBounds,
    ModelRetentionItem,
    ModelRetentionKept,
    ModelRunnerGroup,
    ModelRunnerMount,
    ModelUvBucketBound,
    ModelUvCache,
)
from omnimarket.nodes.node_temp_retention_effect.handlers.handler_temp_retention import (
    ProcessReferences,
    _held_names,
    _remove,
    _scan,
    tree_bytes,
)

NODE_NAME = "node_host_retention_effect"
OVERLAY_ENV = "ONEX_HOST_RETENTION_OVERLAY"
CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"

_RUNNER_ID = re.compile(r"[A-Za-z0-9_-]+")
RUNNER_WORK = "runner_work"
UV_CACHE = "uv_cache"


class RefusalError(Exception):
    """The overlay or contract declares something unsafe; nothing was read or removed."""


def load_contract(contract_path: Path = CONTRACT_PATH) -> dict[str, Any]:
    document = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise RefusalError(f"{contract_path}: the contract must be a mapping")
    return document


def load_bounds(contract_path: Path = CONTRACT_PATH) -> ModelRetentionBounds:
    return ModelRetentionBounds.model_validate(
        load_contract(contract_path)["retention_bounds"]
    )


def load_overlay(overlay_file: str = "") -> ModelHostRetentionOverlay:
    """Read the deployment overlay: the pinned file, else the environment pin, else the overlay roots."""
    if overlay_file:
        try:
            document = yaml.safe_load(Path(overlay_file).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise RefusalError(
                f"overlay {overlay_file} is unreadable ({type(exc).__name__})"
            ) from None
        if not isinstance(document, dict):
            raise RefusalError(
                f"overlay {overlay_file} must be a mapping, so no other overlay source is tried"
            )
        return load_structured_overlay(
            NODE_NAME, ModelHostRetentionOverlay, explicit=document
        )
    return load_structured_overlay(
        NODE_NAME, ModelHostRetentionOverlay, file_env=OVERLAY_ENV
    )


def host_row(overlay: ModelHostRetentionOverlay, host_name: str) -> ModelHostRetention:
    names = [row.name for row in overlay.hosts]
    if len(set(names)) != len(names):
        raise RefusalError("a host is declared more than once in the overlay")
    for row in overlay.hosts:
        if row.name == host_name:
            return row
    raise RefusalError(
        f"{host_name}: not in the {NODE_NAME} overlay, so no retention is declared for it"
    )


def _check_declared_path(path: str, leaf_names: tuple[str, ...], what: str) -> Path:
    declared = Path(path)
    if not declared.is_absolute() or os.path.normpath(path) != path or path == "/":
        raise RefusalError(f"{what} {path!r} must be an absolute, normalised path")
    if declared.name not in leaf_names:
        raise RefusalError(
            f"{what} {path!r} is named {declared.name!r}; the contract allows only "
            f"{', '.join(leaf_names)}"
        )
    if declared.is_symlink() or os.path.realpath(path) != path:
        raise RefusalError(f"{what} {path!r} is a symlink or sits under one")
    return declared


def validate_declarations(
    row: ModelHostRetention, bounds: ModelRetentionBounds
) -> list[str]:
    """Every refusal text for a host row (empty when valid)."""
    refusals: list[str] = []
    for age in row.age_classes:
        try:
            _check_declared_path(
                age.root, bounds.root_leaf_names[age.kind], "age class root"
            )
        except RefusalError as exc:
            refusals.append(str(exc))
        if age.keep_days < bounds.min_keep_days:
            refusals.append(
                f"{age.name}: keep_days {age.keep_days} is below the contract minimum "
                f"{bounds.min_keep_days}"
            )
        if (
            age.kind == "pre_commit_store"
            and age.index_db is not None
            and Path(age.index_db).parent != Path(age.root)
        ):
            refusals.append(
                f"{age.name}: index_db {age.index_db!r} must sit directly inside the root"
            )
    for group in row.runner_groups:
        for runner_id in group.ids:
            if not _RUNNER_ID.fullmatch(runner_id):
                refusals.append(f"runner id {runner_id!r} must match [A-Za-z0-9_-]+")
        for mount in group.mounts:
            for runner_id in group.ids:
                host_root = mount.host_root_template.format(id=runner_id)
                try:
                    _check_declared_path(
                        host_root,
                        bounds.root_leaf_names["runner_mount"],
                        "runner host root",
                    )
                except RefusalError as exc:
                    refusals.append(str(exc))
            container_root = mount.container_root
            parts = container_root.split("/")
            if (
                container_root.startswith("/")
                or ".." in parts
                or os.path.normpath(container_root) != container_root
                or not any(
                    container_root == allowed
                    or container_root.startswith(allowed + "/")
                    for allowed in bounds.allowed_container_roots
                )
            ):
                refusals.append(
                    f"container root {container_root!r} is not under an allowed container root"
                )
    for cache in row.uv_caches:
        try:
            _check_declared_path(
                cache.path, bounds.root_leaf_names["uv_cache"], "uv cache path"
            )
        except RefusalError as exc:
            refusals.append(str(exc))
        cap = cache.cap
        if cap is not None:
            for bucket in cap.buckets:
                if bucket not in bounds.uv_evictable_buckets:
                    refusals.append(
                        f"uv cache {cache.path}: cap bucket {bucket!r} is not named by "
                        f"the contract, which allows only "
                        f"{', '.join(sorted(bounds.uv_evictable_buckets)) or 'none'}"
                    )
            if cap.keep_days < bounds.min_keep_days:
                refusals.append(
                    f"uv cache {cache.path}: cap keep_days {cap.keep_days} is below the "
                    f"contract minimum {bounds.min_keep_days}"
                )
    return refusals


_GIB = 2**30
_DAY = 86400


@dataclass
class _Pass:
    """What one pass has found so far; every list is exact, nothing is capped."""

    items: list[ModelRetentionItem] = field(default_factory=list)
    kept: list[ModelRetentionKept] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    removed: dict[str, int] = field(default_factory=dict)
    freed: dict[str, int] = field(default_factory=dict)
    ran: list[str] = field(default_factory=list)

    def keep(self, class_name: str, path: str, reason: str) -> None:
        self.kept.append(
            ModelRetentionKept(class_name=class_name, path=path, reason=reason)
        )

    def propose(self, item: ModelRetentionItem) -> None:
        self.items.append(item)

    def done(self, class_name: str, freed: int) -> None:
        self.removed[class_name] = self.removed.get(class_name, 0) + 1
        self.freed[class_name] = self.freed.get(class_name, 0) + freed

    def begin(self, class_name: str) -> None:
        if class_name not in self.ran:
            self.ran.append(class_name)


def _held_by_mount(entry: Path, sources: set[str]) -> bool:
    """A running container bind-mounts the entry or something inside it."""
    text = str(entry)
    return any(source == text or source.startswith(text + "/") for source in sources)


def _age_class(
    age: ModelAgeClass,
    bounds: ModelRetentionBounds,
    *,
    now: float,
    uid: int,
    dry_run: bool,
    refs: ProcessReferences,
    live: set[str],
    mounts: set[str],
    unit_refs: ProcessReferences,
    store: PreCommitStorePort,
    out: _Pass,
) -> None:
    root = Path(age.root)
    out.begin(age.name)
    if not root.is_dir():
        out.errors.append(f"{age.root}: absent")
        return
    cutoff = now - age.keep_days * _DAY
    held = _held_names(root, refs)
    unit_held = _held_names(root, unit_refs)
    device = root.lstat().st_dev
    protected = [re.compile(pattern) for pattern in age.protected_name_patterns]
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        try:
            info = entry.lstat()
        except OSError:
            continue
        path = str(entry)
        reason: str | None = None
        if info.st_uid != uid:
            reason = "foreign_owner"
        elif (
            (
                age.kind == "pre_commit_store"
                and (entry.is_symlink() or not entry.is_dir())
            )
            or any(p.fullmatch(entry.name) for p in protected)
            or (entry.name in bounds.protected_leaf_names_always)
        ):
            reason = "protected"
        elif entry.name in held:
            reason = "held_by_process"
        elif entry.name in unit_held:
            reason = "referenced_by_unit"
        elif _held_by_mount(entry, mounts):
            reason = "mounted_by_container"
        elif any(lane in entry.name for lane in live):
            reason = "live_lane"
        else:
            scan = _scan(entry, device, cutoff)
            if scan.special:
                reason = "special_file"
            elif scan.newest >= cutoff:
                reason = "too_new"
        if reason is not None:
            out.keep(age.name, path, reason)
            continue
        size = tree_bytes(entry)
        out.propose(
            ModelRetentionItem(
                class_name=age.name, path=path, bytes=size, action="remove"
            )
        )
        if dry_run:
            continue
        if age.kind == "pre_commit_store" and age.index_db is not None:
            try:
                result = store.forget(age.index_db, path)
            except OSError as exc:
                out.errors.append(f"{entry}: {type(exc).__name__}")
                continue
            if result == "locked":
                out.keep(age.name, path, "store_locked")
                continue
        try:
            _remove(entry, uid)
        except OSError as exc:
            out.errors.append(f"{entry}: {type(exc).__name__}")
            continue
        out.done(age.name, size)


def _runner_mount(
    group: ModelRunnerGroup,
    mount: ModelRunnerMount,
    runner_id: str,
    bounds: ModelRetentionBounds,
    *,
    dry_run: bool,
    ports: HostRetentionPorts,
    out: _Pass,
) -> None:
    container = group.container_template.format(id=runner_id)
    host_root = Path(mount.host_root_template.format(id=runner_id))
    if not host_root.is_dir():
        out.errors.append(f"{host_root}: absent")
        return
    candidates: list[tuple[Path, int]] = []
    for entry in sorted(host_root.iterdir(), key=lambda item: item.name):
        if entry.name in mount.protected_names or (
            entry.name in bounds.protected_leaf_names_always
        ):
            out.keep(RUNNER_WORK, str(entry), "protected")
        elif any(fnmatch.fnmatch(entry.name, pat) for pat in mount.disposable_patterns):
            candidates.append((entry, tree_bytes(entry)))
    budget = mount.keep_gib * _GIB
    remaining = sum(size for _, size in candidates)
    evicting = True
    for entry, size in sorted(candidates, key=lambda item: (-item[1], item[0].name)):
        if remaining <= budget or not evicting:
            out.keep(RUNNER_WORK, str(entry), "under_budget")
            continue
        container_path = str(PurePosixPath(mount.container_root) / entry.name)
        if not dry_run:
            # The state is read again before each removal: a job may have started since the last read.
            if ports.runner_state.is_idle(container) is not True:
                out.keep(RUNNER_WORK, container, "runner_busy")
                evicting = False
                continue
            try:
                ports.runner_remove.remove(
                    container, group.container_user, container_path
                )
            except OSError as exc:
                out.errors.append(f"{entry}: {type(exc).__name__}")
                continue
            if os.path.lexists(entry):
                out.errors.append(f"{entry}: still present")
                continue
            out.done(RUNNER_WORK, size)
        out.propose(
            ModelRetentionItem(
                class_name=RUNNER_WORK,
                path=str(entry),
                bytes=size,
                action="evict",
                reason=f"{container}:{container_path}",
            )
        )
        remaining -= size


def _runner_group(
    group: ModelRunnerGroup,
    bounds: ModelRetentionBounds,
    *,
    dry_run: bool,
    ports: HostRetentionPorts,
    out: _Pass,
) -> None:
    out.begin(RUNNER_WORK)
    for runner_id in group.ids:
        container = group.container_template.format(id=runner_id)
        idle = ports.runner_state.is_idle(container)
        if idle is None:
            out.keep(RUNNER_WORK, container, "runner_state_unknown")
        elif idle is False:
            out.keep(RUNNER_WORK, container, "runner_busy")
        else:
            for mount in group.mounts:
                _runner_mount(
                    group,
                    mount,
                    runner_id,
                    bounds,
                    dry_run=dry_run,
                    ports=ports,
                    out=out,
                )


def _uv_cache(
    cache: ModelUvCache,
    *,
    now: float,
    dry_run: bool,
    buckets: dict[str, ModelUvBucketBound],
    refs: ProcessReferences | None,
    ports: HostRetentionPorts,
    out: _Pass,
) -> None:
    out.begin(UV_CACHE)
    if not Path(cache.path).is_dir():
        out.errors.append(f"{cache.path}: absent")
        return
    try:
        size = ports.uv_cache.size_bytes(
            cache.uv_binary, cache.path, cache.timeout_seconds
        )
    except OSError as exc:
        out.errors.append(f"{cache.path}: {type(exc).__name__}")
        return
    if size < cache.min_gib * _GIB:
        # Below min_gib the cache is also under cap_gib (the model validates cap_gib >= min_gib),
        # so the cap pass has nothing to do here either.
        out.keep(UV_CACHE, cache.path, "below_min")
        return
    out.propose(
        ModelRetentionItem(
            class_name=UV_CACHE,
            path=cache.path,
            bytes=size,
            action="uv_prune",
            reason="uv cache prune; the bytes it frees are measured by the real run",
        )
    )
    if dry_run:
        # The prune never ran, so the cap pass measures with the size read before it.
        evict_over_cap(
            cache,
            size_after=size,
            now=now,
            dry_run=True,
            buckets=buckets,
            refs=refs,
            ports=ports,
            out=out,
        )
        return
    after = size
    try:
        ports.uv_cache.prune(
            cache.uv_binary, cache.path, cache.ci_prune, cache.timeout_seconds
        )
        after = ports.uv_cache.size_bytes(
            cache.uv_binary, cache.path, cache.timeout_seconds
        )
    except OSError as exc:
        # The cap has its own guards and does not depend on the prune: it measures from the size read before the prune.
        out.errors.append(f"{cache.path}: {type(exc).__name__}")
    else:
        out.done(UV_CACHE, max(0, size - after))
    evict_over_cap(
        cache,
        size_after=after,
        now=now,
        dry_run=False,
        buckets=buckets,
        refs=refs,
        ports=ports,
        out=out,
    )


def _disk_readings(
    paths: Iterable[str], ports: HostRetentionPorts
) -> tuple[ModelDiskReading, ...]:
    """One reading per filesystem among the existing declared paths."""
    seen: dict[int, ModelDiskReading] = {}
    for path in sorted(paths):
        try:
            device = os.stat(path).st_dev
            if device not in seen:
                seen[device] = ports.disk.reading(path)
        except OSError:
            continue
    return tuple(seen.values())


def _declared_paths(row: ModelHostRetention) -> list[str]:
    paths = [age.root for age in row.age_classes]
    for group in row.runner_groups:
        for mount in group.mounts:
            paths.extend(mount.host_root_template.format(id=i) for i in group.ids)
    paths.extend(cache.path for cache in row.uv_caches)
    return paths


class HandlerHostRetention:
    """``handle(request) -> result``: read the overlay row of the host and run one pass."""

    def __init__(
        self,
        *,
        now: Callable[[], float] = time.time,
        references: Callable[[], ProcessReferences | None] = read_references_portable,
        ports: HostRetentionPorts | None = None,
        uid: int | None = None,
    ) -> None:
        self.now = now
        self.references = references
        self.ports = ports
        self.uid = uid if uid is not None else os.getuid()

    def _refused(
        self, request: ModelHostRetentionRequest, *texts: str
    ) -> ModelHostRetentionResult:
        return ModelHostRetentionResult(
            host=request.host_name,
            dry_run=request.dry_run,
            status="refused",
            refusals=tuple(texts),
        )

    def handle(self, request: ModelHostRetentionRequest) -> ModelHostRetentionResult:
        try:
            bounds = load_bounds()
            row = host_row(load_overlay(request.overlay_file), request.host_name)
            refusals = validate_declarations(row, bounds)
        except (RefusalError, ValidationError, NodeOverlayError) as exc:
            return self._refused(request, str(exc)[:300])
        if refusals:
            return self._refused(request, *refusals)
        ports = self.ports or HostRetentionPorts.default(row.docker_timeout_seconds)
        wanted = set(request.classes)

        def runs(kind: str) -> bool:
            return not wanted or kind in wanted

        out = _Pass()
        before = _disk_readings(_declared_paths(row), ports)
        now = self.now()
        unreadable = 0
        age_classes = [age for age in row.age_classes if runs(age.kind)]
        # The uv cache cap pass guards its evictions with the same process references the age
        # classes read, so it is read once for both (and not at all when neither needs it).
        wants_uv_cap = runs("uv_cache") and any(
            cache.cap is not None for cache in row.uv_caches
        )
        refs = self.references() if (age_classes or wants_uv_cap) else None
        if age_classes:
            live = ports.lane_liveness.live_lanes(row.lane_run_roots, row.receipt_roots)
            mounts = (
                ports.container_mounts.mount_sources()
                if row.docker_mount_guard
                else set()
            )
            # A guard that cannot be read refuses the age classes: nothing is removed on a guess.
            units = ports.unit_files.unit_texts(row.unit_roots)
            for text, unread in (
                ("process table unreadable", refs is None),
                ("unit files unreadable", units is None),
                ("lane run roots and receipts unreadable", live is None),
                ("container mounts unreadable", mounts is None),
            ):
                if unread:
                    out.errors.append(text)
            if (
                refs is not None
                and live is not None
                and mounts is not None
                and units is not None
            ):
                unreadable = len(refs.unreadable)
                for age in age_classes:
                    _age_class(
                        age,
                        bounds,
                        now=now,
                        uid=self.uid,
                        dry_run=request.dry_run,
                        refs=refs,
                        live=live,
                        mounts=mounts,
                        unit_refs=ProcessReferences(blobs=units),
                        store=ports.pre_commit_store,
                        out=out,
                    )
        if runs("runner_work"):
            for group in row.runner_groups:
                _runner_group(
                    group,
                    bounds,
                    dry_run=request.dry_run,
                    ports=ports,
                    out=out,
                )
        if runs("uv_cache"):
            for cache in row.uv_caches:
                _uv_cache(
                    cache,
                    now=now,
                    dry_run=request.dry_run,
                    buckets=bounds.uv_evictable_buckets,
                    refs=refs,
                    ports=ports,
                    out=out,
                )
        totals = tuple(
            ModelClassTotals(
                class_name=name,
                would_remove_count=sum(i.class_name == name for i in out.items),
                would_remove_bytes=0
                if name == UV_CACHE
                else sum(i.bytes for i in out.items if i.class_name == name),
                removed_count=out.removed.get(name, 0),
                freed_bytes=out.freed.get(name, 0),
                kept_count=sum(k.class_name == name for k in out.kept),
            )
            for name in out.ran
        )
        return ModelHostRetentionResult(
            host=request.host_name,
            dry_run=request.dry_run,
            status="ok",
            items=tuple(sorted(out.items, key=lambda i: (i.class_name, i.path))),
            kept=tuple(out.kept),
            totals=totals,
            total_bytes=sum(i.bytes for i in out.items if i.class_name != UV_CACHE),
            disk_before=before,
            disk_after=()
            if request.dry_run
            else _disk_readings(_declared_paths(row), ports),
            errors=tuple(out.errors),
            unreadable_process_count=unreadable,
        )
