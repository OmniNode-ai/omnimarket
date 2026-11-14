# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Observation stores of node_dev_roll_decision_request_effect (OMN-24251).

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
from omnimarket.nodes.node_dev_roll_decision_request_effect.models import (
    ModelDevRollDecisionRequestPolicy,
    ModelDevRollInMemoryObservationStore,
    ModelDevRollObservationState,
)
from omnimarket.nodes.node_dev_roll_decision_request_effect.protocols import (
    ProtocolDevRollObservationStore,
)

_STATE_NAME = "observation-state.json"


class InMemoryDevRollObservationStore:
    """One state held in the process; the honest one for tests."""

    def __init__(self) -> None:
        self._state = ModelDevRollObservationState()

    def load(self) -> ModelDevRollObservationState:
        return self._state

    def save(self, state: ModelDevRollObservationState) -> None:
        self._state = state


class FileDevRollObservationStore:
    """File store: one JSON document, atomic write (temp file, os.replace)."""

    def __init__(self, root: Path) -> None:
        self._path = root / _STATE_NAME

    def load(self) -> ModelDevRollObservationState:
        """The stored state; an empty state when no document exists yet.

        A document that cannot be read or does not validate raises
        :class:`ValueError`: a broken state must not read as an empty one.
        """
        if not self._path.is_file():
            return ModelDevRollObservationState()
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"observation state {self._path} is unreadable ({type(exc).__name__})"
            ) from exc
        if not isinstance(document, dict):
            raise ValueError(f"observation state {self._path} is not a JSON object")
        try:
            return ModelDevRollObservationState.model_validate(document)
        except ValidationError as exc:
            raise ValueError(
                f"observation state {self._path} is invalid for "
                f"{ModelDevRollObservationState.__name__} ({type(exc).__name__})"
            ) from exc

    def save(self, state: ModelDevRollObservationState) -> None:
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
    policy: ModelDevRollDecisionRequestPolicy, environ: Mapping[str, str]
) -> ProtocolDevRollObservationStore:
    """Build the store the overlay's policy names."""
    store = policy.store
    if isinstance(store, ModelDevRollInMemoryObservationStore):
        return InMemoryDevRollObservationStore()
    try:
        root_env = environ[store.home_env]
    except KeyError as exc:
        raise NodeOverlayError(
            f"environment variable {store.home_env!r} is not set"
        ) from exc
    return FileDevRollObservationStore(Path(root_env) / store.relpath)
