# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The injected break of join J7's error drill (OMN-23420): a check-run API that accepts an invalidation and applies nothing."""

from __future__ import annotations

from typing import Any

from omnimarket.nodes.node_lab_check_run_effect.adapters.inmemory_check_run_api import (
    InMemoryCheckRunApi,
)
from omnimarket.nodes.node_lab_check_run_effect.models.model_lab_check_run import (
    ModelCheckRunRecord,
)


class DeafInvalidationCheckRunApi(InMemoryCheckRunApi):
    """The API accepts an update that would invalidate a run (the superseded conclusion) and changes nothing."""

    def __init__(self, superseded_conclusion: str) -> None:
        super().__init__()
        self._superseded_conclusion = superseded_conclusion

    def update_run(self, **kwargs: Any) -> ModelCheckRunRecord:
        current = next(r for r in self.runs if r.check_run_id == kwargs["check_run_id"])
        if kwargs["conclusion"] == self._superseded_conclusion:
            return current
        return super().update_run(**kwargs)


__all__ = ["DeafInvalidationCheckRunApi"]
