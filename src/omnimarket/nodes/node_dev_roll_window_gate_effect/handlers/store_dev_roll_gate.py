# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Gate stores of node_dev_roll_window_gate_effect (OMN-19998).

Two stores behind one protocol: an in-memory state for tests and a file store
that keeps the whole state as one JSON document, written atomically (temp file
in the target directory, then os.replace). ``store_from_policy`` builds the
right one from the overlay's policy.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_dev_roll_window_gate_effect.models import (
    ModelDevRollGateInMemoryStore,
    ModelDevRollGateState,
    ModelDevRollWindowGatePolicy,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.protocols import (
    ProtocolDevRollGateStore,
)

_STATE_NAME = "gate-state.json"


class InMemoryDevRollGateStore:
    """One state held in the process; the honest one for tests."""

    def __init__(self) -> None:
        self._state = ModelDevRollGateState()

    def load(self) -> ModelDevRollGateState:
        return self._state

    def save(self, state: ModelDevRollGateState) -> None:
        self._state = state


class FileDevRollGateStore:
    """File store: one JSON document, atomic write (temp file, os.replace)."""

    def __init__(self, root: Path) -> None:
        self._path = root / _STATE_NAME

    def load(self) -> ModelDevRollGateState:
        """The stored state; an empty state when no document exists yet.

        A document that cannot be read or does not validate raises
        :class:`ValueError`: a broken state must not read as an empty one.
        """
        if not self._path.is_file():
            return ModelDevRollGateState()
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"gate state {self._path} is unreadable ({type(exc).__name__})"
            ) from exc
        if not isinstance(document, dict):
            raise ValueError(f"gate state {self._path} is not a JSON object")
        try:
            return ModelDevRollGateState.model_validate(document)
        except ValidationError as exc:
            raise ValueError(
                f"gate state {self._path} is invalid for "
                f"{ModelDevRollGateState.__name__} ({type(exc).__name__})"
            ) from exc

    def save(self, state: ModelDevRollGateState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = state.model_dump_json().encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(dir=self._path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            os.replace(tmp_name, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise


def store_from_policy(
    policy: ModelDevRollWindowGatePolicy, environ: Mapping[str, str]
) -> ProtocolDevRollGateStore:
    """Build the store the overlay's policy names."""
    store = policy.store
    if isinstance(store, ModelDevRollGateInMemoryStore):
        return InMemoryDevRollGateStore()
    try:
        root_env = environ[store.home_env]
    except KeyError as exc:
        raise NodeOverlayError(
            f"environment variable {store.home_env!r} is not set"
        ) from exc
    return FileDevRollGateStore(Path(root_env) / store.relpath)
