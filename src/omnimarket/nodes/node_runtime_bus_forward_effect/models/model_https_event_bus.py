# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The HTTPS arm of the runtime bus overlay and its request size and time budgets."""

from __future__ import annotations

import re
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BATCH_PREFIX: Final = b'{"records":['
BATCH_SUFFIX: Final = b"]}"
BATCH_OVERHEAD_BYTES: Final = len(BATCH_PREFIX) + len(BATCH_SUFFIX)
_REFERENCE = re.compile(r"[a-z0-9]+[a-z0-9_-]*(?:\.[a-z0-9]+[a-z0-9_-]*)+")


class ModelHttpsEventBus(BaseModel):
    """All the bounds and both binding references for one HTTPS sender."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    type: Literal["https"]
    ingest_url_ref: str
    auth_ref: str
    max_batch_records: int = Field(ge=1)
    max_event_bytes: int = Field(ge=1)
    max_batch_bytes: int = Field(ge=1)
    max_authorization_bytes: int = Field(ge=1)
    max_key_bytes: int = Field(ge=0)
    max_value_bytes: int = Field(ge=0)
    max_record_headers: int = Field(ge=0)
    max_header_name_chars: int = Field(ge=0)
    max_header_value_chars: int = Field(ge=0)
    max_topic_chars: int = Field(ge=1)
    request_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    door_record_confirm_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    request_margin_seconds: float = Field(ge=0, allow_inf_nan=False)
    max_attempts: int = Field(ge=2)
    retry_backoff_seconds: float = Field(ge=0, allow_inf_nan=False)

    @field_validator("ingest_url_ref", "auth_ref")
    @classmethod
    def _dotted_reference(cls, value: str) -> str:
        if _REFERENCE.fullmatch(value) is None:
            raise ValueError("must be a dotted deployment reference")
        return value

    @model_validator(mode="after")
    def _request_bounds(self) -> Self:
        if self.max_batch_bytes < self.max_event_bytes + BATCH_OVERHEAD_BYTES:
            raise ValueError(
                "max_batch_bytes must fit max_event_bytes and the batch envelope"
            )
        confirm_budget = (
            self.max_batch_records * self.door_record_confirm_timeout_seconds
        )
        if self.request_timeout_seconds <= confirm_budget + self.request_margin_seconds:
            raise ValueError(
                "request_timeout_seconds must exceed max_batch_records times "
                "door_record_confirm_timeout_seconds plus request_margin_seconds"
            )
        return self

    @property
    def recovery_budget_seconds(self) -> float:
        """Two full attempts and the backoff between them."""
        return 2 * self.request_timeout_seconds + self.retry_backoff_seconds

    def minimum_flush_seconds(self, max_events: int) -> float:
        # Each maximum-size record may also require one comma. Counting it in
        # every slot gives a conservative bound, with room for at least one record.
        byte_capacity = (self.max_batch_bytes - BATCH_OVERHEAD_BYTES) // (
            self.max_event_bytes + 1
        )
        records_per_request = max(1, min(self.max_batch_records, byte_capacity))
        sub_batches = (max_events + records_per_request - 1) // records_per_request
        return sub_batches * self.recovery_budget_seconds
