# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The memory bars a lab host is admitted against (OMN-17427).

A lab host at 2026-10-22T01:10Z took about 10 lanes while it paged at swap-in 23 MB/s with memory PSI some avg60 1.09: the only paging bar
was swap-in plus swap-out over 100 MB/s, and the throughput tick's host-swapping bound is 20 MB/s of swap-in. Swap-in alone now refuses
at that bound, on the same reading, so a host the tick names as swapping is refused new lanes and workers. A lab host at 2026-10-10T00:33Z
averaged swap-in 12670 KB/s over 90 one-second samples, under the bound, and stays admitted.
"""

from omnimarket.nodes.node_host_memory_admission_compute.models.model_host_memory_admission import (
    ModelHostMemoryAdmissionRequest,
    ModelHostMemoryAdmissionVerdict,
)


class HandlerHostMemoryAdmission:
    def handle(
        self, request: ModelHostMemoryAdmissionRequest
    ) -> ModelHostMemoryAdmissionVerdict:
        r = request
        reasons: list[str] = []
        if r.mem_avail_gb < r.min_free_gb:
            reasons.append(f"mem_avail {r.mem_avail_gb:.1f}G < {r.min_free_gb:.0f}G")
        if r.mem_psi_avg60 is not None and r.mem_psi_avg60 > r.max_mem_psi:
            reasons.append(f"memory PSI {r.mem_psi_avg60:.1f}% > {r.max_mem_psi:.0f}%")
        if (
            r.swap_in_kb_s is not None
            and r.swap_out_kb_s is not None
            and r.swap_in_kb_s + r.swap_out_kb_s > r.max_swap_kb_s
        ):
            reasons.append(
                f"swap traffic {r.swap_in_kb_s + r.swap_out_kb_s:.0f} KB/s "
                f"(in {r.swap_in_kb_s:.0f}, out {r.swap_out_kb_s:.0f}) > {r.max_swap_kb_s:.0f} KB/s"
            )
        if r.swap_in_kb_s is not None and r.swap_in_kb_s > r.max_swap_in_kb_s:
            reasons.append(
                f"swap-in {r.swap_in_kb_s:.0f} KB/s > {r.max_swap_in_kb_s:.0f} KB/s (paging)"
            )
        return ModelHostMemoryAdmissionVerdict(refusals=tuple(reasons))
