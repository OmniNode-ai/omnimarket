"""Thin COMPUTE handler over the shared subcontract render function."""

from __future__ import annotations

from omnimarket.contract_assembly.models import (
    ModelSubcontractFragment,
    ModelSubcontractRenderRequest,
)
from omnimarket.contract_assembly.render import render_subcontract


class HandlerSubcontractRender:
    """Render one subcontract fragment (discriminated by type) with its digest."""

    def handle(
        self, payload: ModelSubcontractRenderRequest
    ) -> ModelSubcontractFragment:
        return render_subcontract(payload)


__all__ = ["HandlerSubcontractRender"]
