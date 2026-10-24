# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and verdict for the host memory admission node."""

from pydantic import BaseModel, ConfigDict


class ModelHostMemoryAdmissionRequest(BaseModel):
    """One host's memory and paging readings and the bars they are judged against.

    ``mem_psi_avg60`` is Linux memory PSI ``some`` avg60 in percent; ``swap_in_kb_s`` and ``swap_out_kb_s`` are the paging rates
    of the probe's quieter one-second sample. None means the probe did not return the reading, and a reading that is None is
    never judged. The ``max_*`` and ``min_free_gb`` fields are the bars, declared once by the caller.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mem_avail_gb: float
    mem_psi_avg60: float | None
    swap_in_kb_s: float | None
    swap_out_kb_s: float | None
    min_free_gb: float
    max_mem_psi: float
    max_swap_kb_s: float
    max_swap_in_kb_s: float


class ModelHostMemoryAdmissionVerdict(BaseModel):
    """Every bar the host is over, in the order they are judged; empty when the host passes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    refusals: tuple[str, ...]
