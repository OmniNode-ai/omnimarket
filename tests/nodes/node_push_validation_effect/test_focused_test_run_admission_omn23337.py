# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23337 -- the focused run's admission probe reads cores, memory and swap activity."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_push_validation_effect.protocols.ephemeral_container_focused_run_subprocess import (
    parse_admission_probe,
)
from omnimarket.nodes.node_push_validation_effect.protocols.protocol_focused_test_run_client import (
    FocusedTestRunInfraError,
)

pytestmark = pytest.mark.unit

PROBE = """1
34.52 40.1 38.0 5/900 123
32
4096
MemTotal: 100663296
MemAvailable: 48234496
pswpin 1000
pswpout 500
pswpin 6000
pswpout 500
"""


def test_the_probe_gives_cores_memory_and_swap_rates_per_second() -> None:
    a = parse_admission_probe(PROBE)
    assert (a.running_task_containers, a.load_one_minute, a.logical_cores) == (
        1,
        34.52,
        32,
    )
    assert a.mem_total_gb == pytest.approx(96.0)
    assert a.mem_avail_gb == pytest.approx(46.0)
    assert a.swap_in_kb_s == pytest.approx(
        5000 * 4
    )  # 5000 pages of 4 KiB over the one-second sample
    assert a.swap_out_kb_s == 0.0


def test_error_a_probe_with_no_memory_reading_is_unreadable_never_judged_on_load_alone() -> (
    None
):
    with pytest.raises(FocusedTestRunInfraError, match="unreadable admission probe"):
        parse_admission_probe("0\n1.0 1.0 1.0 1/1 1\n32\n4096\n")


def test_error_a_probe_with_no_core_count_is_unreadable() -> None:
    with pytest.raises(FocusedTestRunInfraError, match="unreadable admission probe"):
        parse_admission_probe(
            "0\n1.0 1.0 1.0 1/1 1\nnot-a-number\n4096\nMemTotal: 1\nMemAvailable: 1\n"
        )


def test_a_probe_with_no_swap_counters_reports_none() -> None:
    text = "\n".join(PROBE.splitlines()[:6]) + "\n"
    a = parse_admission_probe(text)
    assert a.swap_in_kb_s is None
    assert a.swap_out_kb_s is None
