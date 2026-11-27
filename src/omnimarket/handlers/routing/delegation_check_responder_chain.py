# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation responder chain a delegation check sends its task through (OMN-26043).

No check names one model. A check reads the ordered responder row of the settings store (local
responders, then OpenRouter, then Claude), asks each local responder what it serves, and
sends its task through the least busy local responder that serves a model and declares its window.
A chain with no usable local responder advances to the cloud tiers, which the routing contract
resolves; the check then records that the task left the local tier.

The window of a responder is the one fact a check needs beyond a model id, and servers name it
differently: vLLM reports ``max_model_len``, llama.cpp reports ``meta.n_ctx``. A responder that
declares neither is skipped with the reason ``no-window``, so the chain never admits a server a
check cannot use (the C14 resolve step read only ``max_model_len`` and read red every hour once the
chain fell from a vLLM server to a llama.cpp one).

This module holds the pure resolution. Reading ``/v1/models`` is the caller's port.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.overlay_documents.model_delegation_check_responders_overlay import (
    ModelDelegationCheckResponder,
)

EnumAttemptOutcome = Literal[
    "chosen",
    "usable-not-chosen",
    "no-answer",
    "no-model",
    "no-window",
]


class ModelResponderReading(BaseModel):
    """What one local responder answered on ``/v1/models`` (and, when it reports one, its load)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str = Field(min_length=1)
    answered: bool
    served_model: str | None = None
    max_model_len: int | None = None
    n_ctx: int | None = None
    # Requests in flight on the responder, when it reports them; None when it does not.
    busy: int | None = None


class ModelResponderAttempt(BaseModel):
    """What the chain did with one declared responder."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: Literal["local", "openrouter", "claude"]
    base_url: str | None = None
    outcome: EnumAttemptOutcome | Literal["advanced-to-tier"]
    served_model: str | None = None
    window: int | None = None
    busy: int | None = None


class ModelChainResolution(BaseModel):
    """The responder the chain chose, and every responder it tried on the way."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The tier that carries the task: ``local`` with a base and a window, or the first cloud tier the chain names.
    tier: Literal["local", "openrouter", "claude"] | None
    base_url: str | None = None
    served_model: str | None = None
    window: int | None = None
    attempts: tuple[ModelResponderAttempt, ...] = ()

    @property
    def is_local(self) -> bool:
        return self.base_url is not None


def window_of(reading: ModelResponderReading) -> int | None:
    """The window a responder declares: ``max_model_len`` else ``meta.n_ctx``; None when neither is a positive int."""
    for value in (reading.max_model_len, reading.n_ctx):
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def reading_from_models_body(
    base_url: str, body: Mapping[str, object] | None, *, busy: int | None = None
) -> ModelResponderReading:
    """Build a reading from a parsed ``/v1/models`` body; ``None`` is a responder that did not answer."""
    if body is None:
        return ModelResponderReading(base_url=base_url, answered=False)
    data = body.get("data")
    first = data[0] if isinstance(data, list) and data else None
    if not isinstance(first, dict):
        return ModelResponderReading(base_url=base_url, answered=True, busy=busy)
    served = first.get("id")
    meta = first.get("meta")
    n_ctx = meta.get("n_ctx") if isinstance(meta, dict) else None
    max_len = first.get("max_model_len")
    return ModelResponderReading(
        base_url=base_url,
        answered=True,
        served_model=served if isinstance(served, str) and served else None,
        max_model_len=max_len if isinstance(max_len, int) else None,
        n_ctx=n_ctx if isinstance(n_ctx, int) else None,
        busy=busy,
    )


def _local_attempt(
    reading: ModelResponderReading,
) -> tuple[EnumAttemptOutcome, int | None]:
    if not reading.answered:
        return "no-answer", None
    if not reading.served_model:
        return "no-model", None
    window = window_of(reading)
    if window is None:
        return "no-window", None
    return "chosen", window


def resolve_chain(
    responders: Sequence[ModelDelegationCheckResponder],
    read: Callable[[str], ModelResponderReading],
) -> ModelChainResolution:
    """Choose the least busy usable local responder; else advance to the first cloud tier the chain names.

    A usable local responder answers, serves a model and declares a window. Among usable local responders the
    one reporting the fewest requests in flight wins, a responder that reports no load counts as idle (its load is
    unknown, never assumed high), and the declared order breaks every tie.
    """
    attempts: list[ModelResponderAttempt] = []
    usable: list[tuple[tuple[int, int], ModelResponderReading, int]] = []
    for index, responder in enumerate(r for r in responders if r.names_base):
        assert responder.base_url is not None
        reading = read(responder.base_url)
        outcome, window = _local_attempt(reading)
        if outcome == "chosen" and window is not None:
            usable.append(
                (
                    (reading.busy or 0, index),
                    reading,
                    window,
                )
            )
        attempts.append(
            ModelResponderAttempt(
                tier="local",
                base_url=responder.base_url,
                outcome=outcome,
                served_model=reading.served_model,
                window=window,
                busy=reading.busy,
            )
        )
    if usable:
        usable.sort(key=lambda entry: entry[0])
        _, winner, window = usable[0]
        marked = tuple(
            a.model_copy(
                update={
                    "outcome": (
                        "chosen"
                        if a.base_url == winner.base_url
                        else "usable-not-chosen"
                    )
                }
            )
            if a.outcome == "chosen"
            else a
            for a in attempts
        )
        return ModelChainResolution(
            tier="local",
            base_url=winner.base_url,
            served_model=winner.served_model,
            window=window,
            attempts=marked,
        )
    cloud = [r for r in responders if not r.names_base]
    for responder in cloud:
        attempts.append(
            ModelResponderAttempt(tier=responder.tier, outcome="advanced-to-tier")
        )
    return ModelChainResolution(
        tier=cloud[0].tier if cloud else None, attempts=tuple(attempts)
    )


__all__ = [
    "ModelChainResolution",
    "ModelResponderAttempt",
    "ModelResponderReading",
    "reading_from_models_body",
    "resolve_chain",
    "window_of",
]
