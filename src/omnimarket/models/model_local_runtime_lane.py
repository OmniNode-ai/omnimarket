# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-19750: a local install declares its own runtime lane.

Operator rulings 2026-09-26 (runtime lane overlays plan, task LO6): a runtime
learns which lane it is, and what that lane is for, only from a ``runtime.lane``
overlay document that whoever runs it supplies. No package names a lane and
there is no packaged default. On a local install the machine's owner is the one
who runs it, and the local path must work with no hand-written settings, so
``onex local init`` supplies the two local-home files a runtime reads:

* ``~/.onex/config.yaml`` gains ``config_source: local-home``: the bootstrap fact
  that selects the local-home overlay source. Every other key is kept.
* the ``runtime.lane`` example document omnibase_infra ships
  (:func:`omnibase_infra.examples.config_overlays.read_runtime_lane_example`) is
  written byte for byte to
  ``~/.omninode/config/<environment>/<lane_id>/runtime.lane.json`` at mode 0600,
  the scope-keyed local-home layout the runtime reads.

Nothing here invents a value: the lane id, its roles and its description are the
example's own, validated against core's model before a byte is written. An
existing document is never overwritten. Identical bytes are a no-op, anything
else is a refusal naming the path, and a bootstrap file that already selects
another source is a refusal too, because a deployment reads exactly one source.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Final

from omnibase_core.enums.enum_config_overlay_key import EnumConfigOverlayKey
from omnibase_core.enums.enum_config_overlay_source import EnumConfigOverlaySource
from omnibase_core.errors.model_onex_error import ModelOnexError
from omnibase_core.models.config_overlay import (
    ModelConfigOverlayScope,
    ModelRuntimeLaneDeclaration,
)
from omnibase_infra.cli.store_onex_home_files import StoreOnexHomeFiles
from omnibase_infra.examples.config_overlays import read_runtime_lane_example
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CONFIG_SOURCE_FIELD",
    "LOCAL_OVERLAY_ENVIRONMENT",
    "LocalRuntimeLaneError",
    "ModelLocalRuntimeLane",
    "declare_local_runtime_lane",
    "local_runtime_lane_path",
]

#: The environment scope segment of a local install. The laptop compose profile
#: runs its runtimes with ``ONEX_ENVIRONMENT=local``, so the runtime looks for
#: its lane document under this segment.
LOCAL_OVERLAY_ENVIRONMENT: Final[str] = "local"

#: The ``~/.onex/config.yaml`` field that selects the one overlay source.
CONFIG_SOURCE_FIELD: Final[str] = "config_source"

_DOCUMENT_MODE: Final[int] = 0o600
_DIRECTORY_MODE: Final[int] = 0o700


class LocalRuntimeLaneError(RuntimeError):
    """Raised when init cannot declare the lane without overwriting something."""


class ModelLocalRuntimeLane(BaseModel):
    """What init declared: the lane, where its document is, and its digest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane_id: str = Field(description="The lane the document declares.")
    environment: str = Field(description="The environment scope segment.")
    path: Path = Field(description="The runtime.lane document on this machine.")
    sha256: str = Field(description="sha256 of the document bytes.")
    newly_written: bool = Field(
        description="True only for the call that wrote the document."
    )


def _local_home_root(home: Path) -> Path:
    return home / ".omninode" / "config"


def local_runtime_lane_path(
    *, home: Path, lane_id: str, environment: str = LOCAL_OVERLAY_ENVIRONMENT
) -> Path:
    """Where the runtime reads a ``runtime.lane`` document from local-home.

    ``<home>/.omninode/config/<environment>/<lane>/runtime.lane.json``. The
    scope segments are part of the path because one machine may address several
    lanes; the runtime's local-home source reads the same layout.
    """
    scope = ModelConfigOverlayScope(environment=environment, lane=lane_id)
    key = EnumConfigOverlayKey.RUNTIME_LANE
    return _local_home_root(home) / scope.environment / scope.lane / f"{key.value}.json"


def _select_local_home(home: Path) -> None:
    """Record ``config_source: local-home``, keeping every other key."""
    files = StoreOnexHomeFiles(home / ".onex")
    try:
        document = files.load_config(must_exist=False)
    except ModelOnexError as exc:
        raise LocalRuntimeLaneError(
            f"cannot read {files.config_path}: {exc.message}"
        ) from exc
    wanted = EnumConfigOverlaySource.LOCAL_HOME.value
    current = document.get(CONFIG_SOURCE_FIELD)
    if current == wanted:
        return
    if current is not None:
        raise LocalRuntimeLaneError(
            f"{files.config_path} already selects the overlay source "
            f"{current!r} ({CONFIG_SOURCE_FIELD}). A deployment reads exactly one "
            f"source, so init will not switch it to {wanted!r}. Remove that line "
            "if this install should read its overlay documents from local-home."
        )
    files.write_config({**document, CONFIG_SOURCE_FIELD: wanted})


def _write_new(path: Path, raw: bytes) -> None:
    """Create ``path`` holding ``raw`` at 0600; never replaces an existing file."""
    path.parent.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _DOCUMENT_MODE)
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)
    path.chmod(_DOCUMENT_MODE)


def declare_local_runtime_lane(*, home: Path | None = None) -> ModelLocalRuntimeLane:
    """Write this install's ``runtime.lane`` document and select local-home.

    Idempotent: a second call finds identical bytes and changes nothing.

    Raises:
        LocalRuntimeLaneError: the bootstrap file selects another source, or a
            different document is already at the path, or the existing one is
            readable by group or other (the runtime refuses such a file).
    """
    home_dir = home if home is not None else Path.home()
    raw = read_runtime_lane_example()
    declaration = ModelRuntimeLaneDeclaration.model_validate(json.loads(raw))
    path = local_runtime_lane_path(home=home_dir, lane_id=declaration.lane_id)
    digest = hashlib.sha256(raw).hexdigest()

    newly_written = False
    if path.exists():
        existing = path.read_bytes()
        if existing != raw:
            raise LocalRuntimeLaneError(
                f"{path} already declares this machine's runtime lane with "
                f"different content (sha256 {hashlib.sha256(existing).hexdigest()}, "
                f"the shipped example is {digest}). init does not overwrite a "
                "declaration; edit or remove that file yourself."
            )
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise LocalRuntimeLaneError(
                f"{path} is mode {mode:04o}; the runtime reads only an owner-only "
                f"file. Fix with: chmod 600 {path}"
            )
    _select_local_home(home_dir)
    if not path.exists():
        _write_new(path, raw)
        newly_written = True

    return ModelLocalRuntimeLane(
        lane_id=declaration.lane_id,
        environment=LOCAL_OVERLAY_ENVIRONMENT,
        path=path,
        sha256=digest,
        newly_written=newly_written,
    )
