# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""One model server's waiting-request count, as read at decision time (OMN-26534)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelEndpointLoad(BaseModel):
    """What one declared backend's server reported about its own queue.

    ``waiting`` is the count of requests the server holds without having started
    them (vLLM ``vllm:num_requests_waiting``, llama.cpp
    ``llamacpp:requests_deferred``). ``None`` means the server could not be read
    (unreachable, an error status, or no known counter in its scrape), and
    ``reason`` says which: an unreadable endpoint is never chosen on load while
    a readable one is available.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_ref: str = Field(
        min_length=1, description="The routing backend_ref this reading is for."
    )
    waiting: int | None = Field(
        default=None,
        ge=0,
        description="Requests waiting on the server, or None when it was unreadable.",
    )
    reason: str = Field(
        default="",
        description="Why the server was unreadable; empty when it was read.",
    )

    @property
    def readable(self) -> bool:
        return self.waiting is not None


__all__: list[str] = ["ModelEndpointLoad"]
