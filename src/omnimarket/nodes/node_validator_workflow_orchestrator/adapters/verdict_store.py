# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The verdict reducer's file-backed state store and the overlay's store choice (OMN-23078).

The reducer node is pure; this store does the file I/O, so it lives with the workflow's effects.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from omnimarket.models.model_validator_verdict import (
    ModelVerdictState,
)
from omnimarket.nodes.node_validator_verdict_reducer.adapters.verdict_store import (
    InMemoryVerdictStore,
)
from omnimarket.nodes.node_validator_verdict_reducer.overlay import (
    ModelVerdictReducerOverlay,
)
from omnimarket.nodes.node_validator_verdict_reducer.protocols import (
    ProtocolVerdictStore,
)


class FileVerdictStore:
    """The state as one JSON document, replaced atomically on save."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> ModelVerdictState:
        if not self._path.exists():
            return ModelVerdictState()
        return ModelVerdictState.model_validate_json(
            self._path.read_text(encoding="utf-8")
        )

    def save(self, state: ModelVerdictState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, self._path)


def _in_memory_store(
    overlay: ModelVerdictReducerOverlay, environ: Mapping[str, str]
) -> ProtocolVerdictStore:
    return InMemoryVerdictStore()


def _file_store(
    overlay: ModelVerdictReducerOverlay, environ: Mapping[str, str]
) -> ProtocolVerdictStore:
    path = environ.get(overlay.store.path_env)
    if not path:
        raise ValueError(
            f"store.backend=file needs {overlay.store.path_env} set to the state file path"
        )
    return FileVerdictStore(Path(path))


# The overlay's store.backend value keys the builder, so no code compares it with a literal.
_STORE_BUILDERS = {"in_memory": _in_memory_store, "file": _file_store}


def store_from_overlay(
    overlay: ModelVerdictReducerOverlay, environ: Mapping[str, str] | None = None
) -> ProtocolVerdictStore:
    """The store the overlay names; the file backend needs its path env var set."""
    env = os.environ if environ is None else environ
    return _STORE_BUILDERS[overlay.store.backend](overlay, env)


def make_reducer_file_backend(workdir: Path) -> ProtocolVerdictStore:
    """The verdict reducer's real backend for the two-backend suite."""
    return FileVerdictStore(workdir / "validator-verdict-state.json")
