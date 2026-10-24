# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host memory admission node judges one bar per reading (OMN-17427).

A lab host at 2026-10-22T01:10Z took about 10 lanes while it paged at swap-in 23 MB/s, memory PSI some avg60 1.09 and 45 GB available:
the installed bar refused only at swap-in plus swap-out over 100 MB/s. Each test here puts one reading over its own bar with every
other reading under theirs, so a bar that stops refusing fails exactly one test.
"""

from __future__ import annotations

from omnimarket.nodes.node_host_memory_admission_compute import (
    HandlerHostMemoryAdmission,
)
from omnimarket.nodes.node_host_memory_admission_compute.models import (
    ModelHostMemoryAdmissionRequest,
)


def replace(
    request: ModelHostMemoryAdmissionRequest, **updates: float | None
) -> ModelHostMemoryAdmissionRequest:
    return request.model_copy(update=updates)


BARS = {
    "min_free_gb": 10.0,
    "max_mem_psi": 5.0,
    "max_swap_kb_s": 100000.0,
    "max_swap_in_kb_s": 20000.0,
}
QUIET = ModelHostMemoryAdmissionRequest(
    mem_avail_gb=45.7,
    mem_psi_avg60=1.09,
    swap_in_kb_s=3000.0,
    swap_out_kb_s=900.0,
    **BARS,
)


def refusals(request: ModelHostMemoryAdmissionRequest) -> tuple[str, ...]:
    return HandlerHostMemoryAdmission().handle(request).refusals


def test_a_quiet_host_over_every_floor_is_admitted() -> None:
    assert refusals(QUIET) == ()


def test_memory_under_the_floor_is_refused() -> None:
    assert refusals(replace(QUIET, mem_avail_gb=9.9)) == ("mem_avail 9.9G < 10G",)
    assert refusals(replace(QUIET, mem_avail_gb=10.0)) == (), "the floor is inclusive"


def test_memory_psi_over_its_bar_is_refused() -> None:
    assert refusals(replace(QUIET, mem_psi_avg60=7.6)) == ("memory PSI 7.6% > 5%",)
    assert refusals(replace(QUIET, mem_psi_avg60=5.0)) == (), "the PSI bar is inclusive"
    assert refusals(replace(QUIET, mem_psi_avg60=None)) == (), (
        "no PSI reading is never judged"
    )


def test_swap_traffic_over_its_bar_is_refused() -> None:
    both = replace(QUIET, swap_in_kb_s=19000.0, swap_out_kb_s=90000.0)
    assert refusals(both) == (
        "swap traffic 109000 KB/s (in 19000, out 90000) > 100000 KB/s",
    )
    assert (
        refusals(replace(QUIET, swap_in_kb_s=20000.0, swap_out_kb_s=80000.0)) == ()
    ), "the traffic bar is inclusive"
    assert refusals(replace(QUIET, swap_in_kb_s=None, swap_out_kb_s=None)) == (), (
        "no swap counters are never judged"
    )


def test_swap_in_alone_over_the_host_swapping_bound_is_refused() -> None:
    # a lab host at 2026-10-22T01:10Z: swap-in 23 MB/s, no swap-out, traffic far under 100 MB/s.
    paging = replace(QUIET, swap_in_kb_s=23000.0, swap_out_kb_s=0.0)
    assert refusals(paging) == ("swap-in 23000 KB/s > 20000 KB/s (paging)",)
    assert refusals(replace(paging, swap_in_kb_s=20000.0)) == (), (
        "the swap-in bar is inclusive"
    )
    assert (
        refusals(replace(paging, swap_in_kb_s=12670.0, swap_out_kb_s=18216.0)) == ()
    ), "a lab host 2026-10-10T00:33Z average"


def test_every_bar_over_at_once_names_each_in_judging_order() -> None:
    worst = ModelHostMemoryAdmissionRequest(
        mem_avail_gb=2.0,
        mem_psi_avg60=40.0,
        swap_in_kb_s=60000.0,
        swap_out_kb_s=70000.0,
        **BARS,
    )
    assert [r.split(" ")[0] for r in refusals(worst)] == [
        "mem_avail",
        "memory",
        "swap",
        "swap-in",
    ]


def test_contract_default_handler_resolves_to_the_packaged_handler() -> None:
    import importlib
    from pathlib import Path

    import yaml

    import omnimarket.nodes.node_host_memory_admission_compute as package

    contract = yaml.safe_load(
        (Path(package.__file__).parent / "contract.yaml").read_text()
    )
    assert contract["name"] == "node_host_memory_admission_compute"
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert contract["metadata"]["transport_type"] == "local"
    assert contract["descriptor"]["runtime_profiles"] == []
    assert contract["runtime_dispatch"]["external_trigger"].strip()
    assert "terminal_event" not in contract
    module_name, class_name = contract["handler_routing"]["default_handler"].split(":")
    handler_class = getattr(importlib.import_module(module_name), class_name)
    assert callable(getattr(handler_class(), "handle", None))
