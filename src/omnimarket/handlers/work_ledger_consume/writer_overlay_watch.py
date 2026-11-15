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

OMN-24330: a service login such as the dev runtime's own has no person login or device, so it
cannot be a row of the developer principal overlay. Its writers are declared in the append
node's own deployment overlay (``service_writers``), found the way every node overlay is found.
The watch takes that overlay as a second, independent source: each source keeps its own change
token and its own last good writers, so an unreadable or malformed source keeps its previous
writers and a source with no path contributes nothing. The served set is the union of the
install-time writers and both sources' writers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from functools import partial
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omnimarket.handlers.work_ledger_consume.service_writers import (
    ModelServiceWritersPolicy,
    load_service_writers_policy,
    service_writers,
    service_writers_path,
)
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
    """Serve the install-time writers plus both overlay sources' writing rows, following each file."""

    def __init__(
        self,
        path: Path | None,
        policy: ModelWriterOverlayPolicy,
        *,
        static_writers: Iterable[str],
        principal_pattern: str,
        apply: ApplyWriters,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        service_locate: Callable[[], Path | None] | None = None,
        service_policy: ModelServiceWritersPolicy | None = None,
    ) -> None:
        self._path = path
        self._policy = policy
        self._static = frozenset(static_writers)
        self._pattern = principal_pattern
        self._apply = apply
        self._sleep = sleep
        self._seen: tuple[int, int] | None = None
        self._task: asyncio.Task[None] | None = None
        self.overlay_writers: frozenset[str] = frozenset()
        self.service_writers: frozenset[str] = frozenset()
        self._service_locate = service_locate
        self._service_policy = service_policy
        self._service_seen: tuple[int, int] | None = None

    @property
    def writers(self) -> frozenset[str]:
        return self._static | self.overlay_writers | self.service_writers

    def _has_source(self) -> bool:
        return self._path is not None or self._service_locate is not None

    def _read_source(
        self,
        path: Path,
        parse: Callable[[str], frozenset[str]],
        seen: tuple[int, int] | None,
        last: frozenset[str],
    ) -> tuple[frozenset[str], tuple[int, int] | None]:
        """Read one source if it changed; keep its last good writers when it cannot be read."""
        try:
            stat = path.stat()
        except OSError as exc:
            logger.warning(
                "work-ledger writer overlay %s unreadable (%s); writers unchanged",
                path,
                exc,
            )
            return last, seen
        token = (stat.st_mtime_ns, stat.st_size)
        if token == seen:
            return last, seen
        try:
            found = parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            logger.error(
                "work-ledger writer overlay %s refused (%s); writers unchanged",
                path,
                exc,
            )
            return last, seen
        if found == last:
            return last, token
        return found, token

    async def refresh(self) -> bool:
        """Read each source if it changed; True when the served writers changed."""
        if not self._has_source():
            return False
        before = self.writers
        if self._path is not None:
            self.overlay_writers, self._seen = self._read_source(
                self._path,
                lambda text: overlay_writers(text, self._policy, self._pattern),
                self._seen,
                self.overlay_writers,
            )
        service_policy = self._service_policy
        if self._service_locate is not None and service_policy is not None:
            path = self._service_locate()
            if path is not None:
                self.service_writers, self._service_seen = self._read_source(
                    path,
                    partial(
                        service_writers,
                        policy=service_policy,
                        principal_pattern=self._pattern,
                    ),
                    self._service_seen,
                    self.service_writers,
                )
        if self.writers == before:
            return False
        added = sorted(self.writers - before)
        removed = sorted(before - self.writers)
        changed = await self._apply(self.writers)
        if changed:
            logger.warning(
                "work-ledger writers: added %s, removed %s",
                added or "none",
                removed or "none",
            )
        return changed

    def start(self) -> None:
        if self._task is None and self._has_source():
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
    With the overlay root unset the watch serves the install-time writers only, and says so.
    """
    policy = load_writer_overlay_policy()
    path = overlay_path(policy, environment)
    if path is None:
        logger.warning(
            "%s is unset: serving the install-time writers only", policy.root_env
        )
    service_policy = load_service_writers_policy()

    def service_locate() -> Path | None:
        return service_writers_path(service_policy, environment)

    async def apply(writers: frozenset[str]) -> bool:
        handler.writers.replace(writers)
        return await host.set_writer_principals(writers)

    watch = WriterOverlayWatch(
        path,
        policy,
        static_writers=static_writers,
        principal_pattern=host.topics.writer_principal_pattern,
        apply=apply,
        service_locate=service_locate,
        service_policy=service_policy,
    )
    await watch.refresh()
    watch.start()
    return watch
