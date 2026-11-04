# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the pre-push stage hooks said about a pending push range."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, computed_field

from omnimarket.nodes.node_prepush_preflight_effect.models.model_prepush_refusal import (
    ModelPrepushRefusal,
)


class ModelPrepushPreflightResult(BaseModel):
    """``status``: ``ok`` every pre-push hook passed, ``refused`` at least one refused (``refusals``),
    ``no-config`` no pre-commit configuration, ``no-range`` no range could be named, ``empty-range`` nothing
    is pending, ``error`` the hooks could not be run (``error_tail``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["ok", "refused", "no-config", "no-range", "empty-range", "error"]
    range_from: str | None = None
    range_to: str | None = None
    refusals: tuple[ModelPrepushRefusal, ...] = ()
    error_tail: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def report(self) -> dict[str, Any]:
        """The keys a lane's hook-preflight result takes from this answer: the range and status always, and, when a
        hook refused, the ``failing`` outcome with the first hook id, every hook id and each refusal's finding."""
        summary: dict[str, Any] = {
            "prepush": {
                "status": self.status,
                "range_from": self.range_from,
                "range_to": self.range_to,
            }
        }
        if not self.refusals:
            return summary
        tail = "\n".join(
            f"pre-push {r.hook_id} (exit {r.exit_code}): {r.finding}"
            for r in self.refusals
        )
        return {
            **summary,
            "outcome": "failing",
            "hook": self.refusals[0].hook_id,
            "failing": [r.hook_id for r in self.refusals],
            "error_tail": tail,
        }
