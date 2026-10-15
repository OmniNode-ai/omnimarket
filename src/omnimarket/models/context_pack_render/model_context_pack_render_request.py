# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request to render a context pack into messages (OMN-21109, plan task TA.7)."""

from __future__ import annotations

from omnibase_core.models.pack.model_context_pack import ModelContextPack
from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.models.context_pack_render.model_context_pack_carried_prompt import (
    ModelContextPackCarriedPrompt,
)
from omnimarket.models.context_pack_render.model_context_pack_render_chunk_ref import (
    ModelContextPackRenderChunkRef,
)
from omnimarket.models.context_pack_render.model_context_pack_render_identity import (
    ModelContextPackRenderIdentity,
)


class ModelContextPackRenderRequest(BaseModel):
    """A pack, the source identity of its chunks, the build identity and the carried prompts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pack: ModelContextPack
    chunk_refs: tuple[ModelContextPackRenderChunkRef, ...] = ()
    identity: ModelContextPackRenderIdentity
    carried_prompts: tuple[ModelContextPackCarriedPrompt, ...] = ()

    @model_validator(mode="after")
    def _ids_are_unique(self) -> ModelContextPackRenderRequest:
        for label, ids in (
            ("chunk ref", [r.chunk_id for r in self.chunk_refs]),
            ("carried prompt", [p.prompt_id for p in self.carried_prompts]),
        ):
            repeated = sorted({i for i in ids if ids.count(i) > 1})
            if repeated:
                raise ValueError(f"{label} id repeated: {', '.join(repeated)}")
        return self
