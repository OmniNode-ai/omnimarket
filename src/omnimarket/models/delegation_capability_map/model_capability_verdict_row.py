# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One rubric verdict or label on one answer, the unit the capability map folds."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
)

from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.delegation_capability_map.enum_capability_source import (
    EnumCapabilitySource,
)
from omnimarket.models.delegation_capability_map.model_capability_configuration import (
    ModelCapabilityConfiguration,
)
from omnimarket.models.delegation_capability_map.model_capability_identity import (
    ModelCapabilityIdentity,
)


class ModelCapabilityVerdictRow(BaseModel):
    """A pass or fail on one item, answered by one model under one configuration.

    ``passed`` is a rubric pass on an accepted answer, or a hand, blind or DoD
    label of adequate. ``configuration`` is None when the producer recorded no
    configuration, and such a row folds into no cell.

    ``host`` is the machine the backend's endpoint lives on and
    ``answering_backend`` the backend that returned the answer. A graded run
    records both for every row; a producer that did not record them leaves
    them None.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    item_key: str = Field(min_length=1)
    source: EnumCapabilitySource
    task_class: str = Field(min_length=1)
    size_band: EnumSizeBand
    model_key: str = Field(min_length=1)
    backend_id: str = Field(min_length=1)
    configuration: ModelCapabilityConfiguration | None = None
    identity: ModelCapabilityIdentity
    passed: bool
    host: str | None = Field(default=None, min_length=1)
    answering_backend: str | None = Field(default=None, min_length=1)

    @model_serializer(mode="wrap")
    def _omit_unrecorded_serving_fields(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """A row whose producer recorded no serving machine keeps its prior wire shape."""
        dumped: dict[str, Any] = handler(self)
        for name in ("host", "answering_backend"):
            if dumped.get(name) is None:
                dumped.pop(name, None)
        return dumped
