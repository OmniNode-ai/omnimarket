# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerInstallReading: piece 10's reading at a read time (OMN-22956).

For every required host the newest install-canary probe is judged against the request's own time and max age:

* it exists, is not future-dated and is inside its max age;
* the head it read installs at least one file, and no file of that head is missing from the host's copy;
* a drill probe, which deletes ``fault`` from a scratch copy, names that file in ``missing``. A drill
  that does not see its own fault means the probe is blind, which reads as a failure.

A failure of a readable source reads NOT_WORKING. A source that could not be read reads UNKNOWN only
when nothing readable already fails.
"""

from __future__ import annotations

import math
from datetime import timedelta

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_install_reading_compute.models.model_install_reading import (
    ModelInstallProbeFact,
    ModelInstallReading,
    ModelInstallReadingRequest,
)


def judge_probe(
    probe: ModelInstallProbeFact, request: ModelInstallReadingRequest
) -> list[str]:
    """The failures of one host's newest probe."""
    age = request.now - probe.probed_at
    if age < timedelta(0):
        return [f"probe-future-dated:{probe.host}"]
    failures: list[str] = []
    if age > timedelta(seconds=request.probe_max_age_seconds):
        failures.append(
            f"probe-stale:{probe.host}:age-{math.ceil(age.total_seconds())}s"
            f"-over-{request.probe_max_age_seconds}s"
        )
    if probe.expected == 0:
        failures.append(f"file-list-empty:{probe.host}")
    if probe.missing:
        failures.append(
            f"install-missing:{probe.host}:{len(probe.missing)}:{sorted(probe.missing)[0]}"
        )
    if probe.trigger == "drill" and probe.fault not in probe.missing:
        failures.append(f"drill-fault-unseen:{probe.host}:{probe.fault}")
    return failures


def newest_probes(
    request: ModelInstallReadingRequest,
) -> dict[str, ModelInstallProbeFact]:
    """The newest probe of each required host; a probe of any other host is no evidence."""
    newest: dict[str, ModelInstallProbeFact] = {}
    for probe in request.probes:
        if probe.host not in request.required_hosts:
            continue
        held = newest.get(probe.host)
        if held is None or (probe.probed_at, probe.trigger) > (
            held.probed_at,
            held.trigger,
        ):
            newest[probe.host] = probe
    return newest


class HandlerInstallReading:
    """COMPUTE handler: no clock read (the request carries the time), no state, no I/O."""

    def handle(self, request: ModelInstallReadingRequest) -> ModelInstallReading:
        failures: list[str] = []
        unread: list[str] = []
        whole = 0
        if request.probes_unread_reason is not None:
            unread.append(f"probes-unreadable:{request.probes_unread_reason}")
        else:
            newest = newest_probes(request)
            for host in request.required_hosts:
                probe = newest.get(host)
                if probe is None:
                    failures.append(f"probe-missing:{host}")
                    continue
                host_failures = judge_probe(probe, request)
                failures += host_failures
                whole += not host_failures
        if failures:
            verdict = EnumReadingVerdict.NOT_WORKING
        elif unread:
            verdict = EnumReadingVerdict.UNKNOWN
        else:
            verdict = EnumReadingVerdict.WORKING
        return ModelInstallReading(
            verdict=verdict,
            reasons=tuple(failures + unread),
            hosts_checked=len(request.required_hosts),
            hosts_whole=whole,
            read_at=request.now,
        )


__all__: list[str] = ["HandlerInstallReading", "judge_probe", "newest_probes"]
