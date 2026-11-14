# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Take the ledger's machine writers from the principal overlay while serving (OMN-21622).

The serve process learned its writers once, from the ``--writer-principal`` list its installer
rendered. A machine declared afterwards (h051's login on 2026-10-30) published its rows on its own
writer topic and nothing consumed them until the serve unit was rendered again. Operator RULING
2026-10-30T21:35:44Z: a machine must not need the same step repeated per machine.

``WriterOverlayWatch`` reads the deployment's developer.principal overlay at start and again
whenever the file changes, and serves the install-time writers plus every row whose role the
append node's contract names (``writer_overlay``). A new writer's topic is subscribed from its
group's committed offset, so rows it published before it was declared are appended then. An
overlay that cannot be read or is malformed keeps the writers already served; it never empties
them. Where the overlay lives, which roles write and how often it is checked are declared in the
contract, not here.

A service login (OMN-24357: the dev runtime's own login) is neither a developer nor a machine row, so
the append node's own overlay declares it as ``service_writers``. That overlay is found under the
declared overlay roots, then in the overlays clone beside the workspace config root, and is followed
the same way: a malformed or deleted file keeps the service writers already served.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omnimarket.handlers.node_overlay_reader import OVERLAY_FILE_NAME, find_node_overlay
from omnimarket.work_ledger_bus.bus import (
    WORK_LEDGER_APPEND_NODE,
    WorkLedgerAppendHost,
    node_contract,
)

logger = logging.getLogger(__name__)

ApplyWriters = Callable[[frozenset[str]], Awaitable[bool]]


class ModelWriterOverlayPolicy(BaseModel):
    """The append node contract's ``writer_overlay`` section."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root_env: str = Field(min_length=1)
    path: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    default_role: str = Field(min_length=1)
    roles: tuple[str, ...] = Field(min_length=1)
    refresh_seconds: float = Field(gt=0)


class ModelServiceWritersPolicy(BaseModel):
    """The append node contract's ``service_writers`` section."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay_id: str = Field(min_length=1)
    clone_dir: str = Field(min_length=1)
    clone_subdir: str = Field(min_length=1)


class ModelServiceWritersOverlay(BaseModel):
    """The append node's overlay: the service logins that write, and nothing else."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    service_writers: tuple[str, ...] = Field(min_length=1)


class ModelWriterOverlayRow(BaseModel):
    """The two fields of an overlay row this watch reads; identity fields are never read."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    principal: str
    role: str | None = None


class ModelWriterOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: str
    principals: tuple[ModelWriterOverlayRow, ...] = Field(min_length=1)


class ProtocolWriterSet(Protocol):
    def replace(self, writers: Iterable[str]) -> None: ...


class ProtocolWriterSetHolder(Protocol):
    @property
    def writers(self) -> ProtocolWriterSet: ...


def load_writer_overlay_policy() -> ModelWriterOverlayPolicy:
    contract = node_contract(WORK_LEDGER_APPEND_NODE)
    return ModelWriterOverlayPolicy.model_validate(contract["writer_overlay"])


def load_service_writers_policy() -> ModelServiceWritersPolicy:
    contract = node_contract(WORK_LEDGER_APPEND_NODE)
    return ModelServiceWritersPolicy.model_validate(contract["service_writers"])


def service_overlay_path(
    policy: ModelServiceWritersPolicy,
    root_env: str,
    environment: Mapping[str, str] | None = None,
) -> Path | None:
    """The service overlay under the declared roots, else in the clone beside the config root.

    None when neither holds the file, which is a state the watch keeps the served writers through.
    """
    env = os.environ if environment is None else environment
    found = find_node_overlay(policy.overlay_id, env)
    if found is not None:
        return found
    config_root = env.get(root_env)
    if not config_root:
        return None
    beside = (
        Path(config_root).parent
        / policy.clone_dir
        / policy.clone_subdir
        / policy.overlay_id
        / OVERLAY_FILE_NAME
    )
    return beside if beside.is_file() else None


def service_writers_of(text: str, principal_pattern: str) -> frozenset[str]:
    """The service logins the overlay declares; ValueError when malformed."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"service overlay is not valid YAML: {exc}") from exc
    try:
        overlay = ModelServiceWritersOverlay.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"service overlay is malformed: {exc}") from exc
    names = overlay.service_writers
    if len(set(names)) != len(names):
        raise ValueError("service overlay declares a login twice")
    for name in names:
        if re.fullmatch(principal_pattern, name) is None:
            raise ValueError(f"service overlay login {name!r} is not a writer name")
    return frozenset(names)


def overlay_path(
    policy: ModelWriterOverlayPolicy, environment: Mapping[str, str] | None = None
) -> Path | None:
    """The overlay under the declared root, or None when the root is not set."""
    root = (os.environ if environment is None else environment).get(policy.root_env)
    if not root:
        return None
    return Path(root) / policy.path


def overlay_writers(
    text: str, policy: ModelWriterOverlayPolicy, principal_pattern: str
) -> frozenset[str]:
    """The principals of the overlay rows whose role writes; ValueError when malformed."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"overlay is not valid YAML: {exc}") from exc
    try:
        overlay = ModelWriterOverlay.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"overlay is malformed: {exc}") from exc
    if overlay.schema_version != policy.schema_version:
        raise ValueError(
            f"overlay schema_version {overlay.schema_version!r} is not {policy.schema_version!r}"
        )
    names = [row.principal for row in overlay.principals]
    if len(set(names)) != len(names):
        raise ValueError("overlay declares a principal twice")
    for name in names:
        if re.fullmatch(principal_pattern, name) is None:
            raise ValueError(f"overlay principal {name!r} is not a writer name")
    return frozenset(
        row.principal
        for row in overlay.principals
        if (row.role or policy.default_role) in policy.roles
    )


class WriterOverlayWatch:
    """Serve the install-time writers plus the overlays' writing rows, following the files."""

    def __init__(
        self,
        path: Path | None,
        policy: ModelWriterOverlayPolicy,
        *,
        static_writers: Iterable[str],
        principal_pattern: str,
        apply: ApplyWriters,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        service_policy: ModelServiceWritersPolicy | None = None,
        service_locator: Callable[[], Path | None] | None = None,
    ) -> None:
        self._path = path
        self._policy = policy
        self._static = frozenset(static_writers)
        self._pattern = principal_pattern
        self._apply = apply
        self._sleep = sleep
        self._service_policy = service_policy
        self._service_locator = service_locator
        self._seen: tuple[int, int] | None = None
        self._service_seen: tuple[Path, int, int] | None = None
        self._service_absent = False
        self._task: asyncio.Task[None] | None = None
        self.overlay_writers: frozenset[str] = frozenset()
        self.service_writers: frozenset[str] = frozenset()

    @property
    def writers(self) -> frozenset[str]:
        return self._static | self.overlay_writers | self.service_writers

    @property
    def _watching(self) -> bool:
        return self._path is not None or self._service_locator is not None

    async def refresh(self) -> bool:
        """Read each overlay that changed; True when the served writers changed."""
        before = self.writers
        self._read_principal_overlay()
        self._read_service_overlay()
        after = self.writers
        if after == before:
            return False
        added = sorted(after - before)
        removed = sorted(before - after)
        changed = await self._apply(after)
        if changed:
            logger.warning(
                "work-ledger writers changed: added %s, removed %s",
                added or "none",
                removed or "none",
            )
        return changed

    def _read_principal_overlay(self) -> None:
        if self._path is None:
            return
        try:
            stat = self._path.stat()
        except OSError as exc:
            logger.warning(
                "work-ledger writer overlay %s unreadable (%s); writers unchanged",
                self._path,
                exc,
            )
            return
        seen = (stat.st_mtime_ns, stat.st_size)
        if seen == self._seen:
            return
        try:
            found = overlay_writers(
                self._path.read_text(encoding="utf-8"), self._policy, self._pattern
            )
        except (OSError, UnicodeError, ValueError) as exc:
            logger.error(
                "work-ledger writer overlay %s refused (%s); writers unchanged",
                self._path,
                exc,
            )
            return
        self._seen = seen
        self.overlay_writers = found

    def _read_service_overlay(self) -> None:
        if self._service_locator is None or self._service_policy is None:
            return
        path = self._service_locator()
        if path is None:
            if not self._service_absent:
                logger.warning(
                    "work-ledger service overlay %s not found; service writers unchanged",
                    self._service_policy.overlay_id,
                )
            self._service_absent = True
            return
        self._service_absent = False
        try:
            stat = path.stat()
        except OSError as exc:
            logger.warning(
                "work-ledger service overlay %s unreadable (%s); service writers unchanged",
                path,
                exc,
            )
            return
        seen = (path, stat.st_mtime_ns, stat.st_size)
        if seen == self._service_seen:
            return
        try:
            found = service_writers_of(path.read_text(encoding="utf-8"), self._pattern)
        except (OSError, UnicodeError, ValueError) as exc:
            logger.error(
                "work-ledger service overlay %s refused (%s); service writers unchanged",
                path,
                exc,
            )
            return
        self._service_seen = seen
        self.service_writers = found

    def start(self) -> None:
        if self._task is None and self._watching:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            await self._sleep(self._policy.refresh_seconds)
            try:
                await self.refresh()
            except Exception:  # one failed retake must not end the watch
                logger.exception("work-ledger writer overlay: refresh failed")


async def follow_overlay_writers(
    host: WorkLedgerAppendHost,
    handler: ProtocolWriterSetHolder,
    static_writers: Iterable[str],
    environment: Mapping[str, str] | None = None,
) -> WriterOverlayWatch:
    """Serve the overlay's writers now and follow the file; the host need not be started.

    The first check of the loop is one refresh interval away, after the serve has subscribed.
    With the overlay root unset the watch takes no machine writers, and says so; the service
    overlay is looked for under the declared overlay roots and beside the config root.
    """
    policy = load_writer_overlay_policy()
    path = overlay_path(policy, environment)
    if path is None:
        logger.warning(
            "%s is unset: no machine writers from the principal overlay",
            policy.root_env,
        )
    service_policy = load_service_writers_policy()

    async def apply(writers: frozenset[str]) -> bool:
        handler.writers.replace(writers)
        return await host.set_writer_principals(writers)

    watch = WriterOverlayWatch(
        path,
        policy,
        static_writers=static_writers,
        principal_pattern=host.topics.writer_principal_pattern,
        apply=apply,
        service_policy=service_policy,
        service_locator=lambda: service_overlay_path(
            service_policy, policy.root_env, environment
        ),
    )
    await watch.refresh()
    watch.start()
    return watch
