# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the model setup effect is asked to do."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

#: The models a developer chooses from, in the order onboarding lists them.
#: ``anthropic`` joined by operator ruling 2026-10-11T15:05:50Z (OMN-20990).
#: ``glm`` (OMN-20157): z.ai's general API, the catalogue's customer GLM plan.
#: ``local`` (OMN-21162): a model server on an OpenAI-compatible URL, no key.
ModelProvider = Literal[
    "gemini",
    "openrouter",
    "openai",
    "anthropic",
    "glm",
    "ollama",
    "local",
    # OMN-21263: any other endpoint that speaks the OpenAI API, by URL, with a key.
    "openai_compatible",
]


class ModelModelSetupRequest(BaseModel):
    """``status``: every provider, set up or not, with its last test.
    ``test``: one pinned delegation for ``provider``, or for every set-up
    provider when it is ``None``. ``forget``: drop ``provider``'s last test,
    after its key is removed.

    OMN-21059, on the ordered models a stored key may serve (``provider`` and
    ``models`` required): ``add_models`` appends ``models`` after the stored
    ones; ``reorder`` replaces the list with ``models``, which must name exactly
    the stored set; ``remove_model`` drops the one model in ``models``.

    OMN-20844 (``provider`` required): ``default_model`` reads the model onboarding
    registers for ``provider`` from the deployment overlay; nothing is stored.

    OMN-21162: ``add_local`` declares the keyless server at ``url`` serving the
    one model in ``models`` as a backend in the local routes overlay;
    ``remove_local`` takes it out again. Other overlay entries are kept.

    OMN-21346: ``set_first`` makes the model in ``models`` (or, with none named,
    ``provider``'s own first model) the one every delegation tries first: it
    moves to the front of the provider's list and is recorded as the choice.
    ``clear_first`` drops the choice, so runs keep their usual order.

    OMN-21267: ``set_only`` records the rule ``only`` (``local`` or ``free``):
    every delegation on this machine skips a model outside it. ``clear_only``
    removes the rule, so routing is what it was."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal[
        "status",
        "test",
        "forget",
        "add_models",
        "reorder",
        "remove_model",
        "default_model",
        "add_local",
        "remove_local",
        "set_first",
        "clear_first",
        "set_only",
        "clear_only",
    ]
    provider: ModelProvider | None = None
    models: tuple[str, ...] = ()
    url: str | None = None
    #: OMN-21267: the rule ``set_only`` records.
    only: Literal["local", "free"] | None = None


__all__ = ["ModelModelSetupRequest", "ModelProvider"]
