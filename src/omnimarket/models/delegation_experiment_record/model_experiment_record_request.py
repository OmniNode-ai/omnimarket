# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The records to check, the experiments the program lists and the receipts that exist."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation_experiment_record.model_experiment_record import (
    ModelExperimentRecord,
)


class ModelExperimentRecordRequest(BaseModel):
    """``known_receipt_ids`` is the set of run ids found in delegation_events or on disk.

    The caller reads that set; the check holds no I/O. ``protected_path_markers`` name the
    dev-lane overlay and the bifrost contract path, and ``retired_models`` the models no
    experiment may measure. The deployment overlay must supply that policy; a
    missing or empty policy fails validation rather than disabling the check.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    listed_experiment_ids: tuple[str, ...]
    records: tuple[ModelExperimentRecord, ...]
    known_receipt_ids: tuple[str, ...] = ()
    protected_path_markers: tuple[str, ...] = (
        "bifrost_delegation",
        "bifrost_overrides",
    )
    retired_models: tuple[str, ...] = Field(min_length=1)
