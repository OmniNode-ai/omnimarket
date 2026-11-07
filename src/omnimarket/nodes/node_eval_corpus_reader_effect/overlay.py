# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_eval_corpus_reader_effect: which corpus backend runs and where its journals sit.

Found at ``<root>/node_eval_corpus_reader_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``.
The package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses
the load and names it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_eval_corpus_reader_effect.protocols import (
    InMemoryEvalCorpus,
    JsonlEvalCorpus,
    ProtocolEvalCorpus,
)

NODE_NAME = "node_eval_corpus_reader_effect"


class ModelEvalCorpusOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: ``in_memory`` keeps the corpus in the process; ``jsonl`` is the durable journal the floor reads.
    backend: Literal["in_memory", "jsonl"]
    #: The variable naming the floor's state directory, the corpus journal's path under it, and the emission
    #: journal's path under it (the floor's receipt fold reads that one).
    state_home_env: str = Field(min_length=1)
    corpus_journal_relpath: str = Field(min_length=1)
    emission_journal_relpath: str = Field(min_length=1)


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelEvalCorpusOverlay:
    return load_structured_overlay(NODE_NAME, ModelEvalCorpusOverlay, environ)


def state_home(overlay: ModelEvalCorpusOverlay, environ: Mapping[str, str]) -> Path:
    value = environ.get(overlay.state_home_env, "").strip()
    if not value:
        raise NodeOverlayError(
            f"{NODE_NAME}: the variable {overlay.state_home_env} names no state directory"
        )
    return Path(value)


def build_corpus(
    overlay: ModelEvalCorpusOverlay, environ: Mapping[str, str]
) -> ProtocolEvalCorpus:
    """The corpus the overlay selects: in memory, or the journal under the floor's state directory."""
    builders: dict[str, Callable[[], ProtocolEvalCorpus]] = {
        "in_memory": InMemoryEvalCorpus,
        "jsonl": lambda: JsonlEvalCorpus(
            state_home(overlay, environ) / overlay.corpus_journal_relpath
        ),
    }
    return builders[overlay.backend]()


__all__ = ["ModelEvalCorpusOverlay", "build_corpus", "load_overlay", "state_home"]
