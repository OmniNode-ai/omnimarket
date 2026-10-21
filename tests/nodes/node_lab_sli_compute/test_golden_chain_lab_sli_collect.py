# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain of the collector: sources -> readings -> verdicts (OMN-17427)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from omnimarket.enums.enum_lab_sli import EnumLabSliStatus
from omnimarket.nodes.node_lab_sli_collect_effect.handlers.handler_lab_sli_collect import (
    HandlerLabSliCollect,
)
from omnimarket.nodes.node_lab_sli_collect_effect.models import (
    ModelLabSliCollectRequest,
)
from omnimarket.nodes.node_lab_sli_collect_effect.protocols import ProbeError
from omnimarket.nodes.node_lab_sli_compute.handlers.handler_lab_sli_evaluate import (
    HandlerLabSliEvaluate,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 17, 3, 0, tzinfo=UTC)

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                             1000000.
Pages active:                           2000000.
Pages inactive:                         2000000.
Pages speculative:                       100000.
Pages purgeable:                          98576.
"""

LAUNCHCTL = (
    "PID\tStatus\tLabel\n"
    "25001\t0\tai.omninode.clone-refresh-serve\n"
    "-\t78\tai.omninode.host-reconcile\n"
    "65085\t3\tai.omninode.host-drift\n"
    "-\t1\tcom.apple.unrelated\n"
    "-\t0\tcom.omninode.gemma4-12b-serve\n"
)


def _pr(
    repo: str, number: int, finished: str, *, draft: bool = False
) -> dict[str, object]:
    return {
        "cls": "green-unarmed",
        "ci": {"verdict": "GREEN", "runs": [["ci", "completed", "success", finished]]},
        "facts": {"state": "OPEN", "draft": draft, "repo": repo, "number": number},
    }


STATE = {
    "prs": {
        "omnimarket#4004": _pr("omnimarket", 4004, "2026-10-17T00:31:00Z"),
        "omnimarket#4010": _pr("omnimarket", 4010, "2026-10-17T02:30:00Z"),
        "omnibase_infra#9": _pr("omnibase_infra", 9, "2026-10-17T02:00:00Z"),
        "omnimarket#1": _pr("omnimarket", 1, "2026-09-01T00:00:00Z", draft=True),
    },
    "merges": {
        "omnimarket#3999": {"repo": "omnimarket", "merged_at": "2026-10-17T01:00:00Z"},
        "omnimarket#3990": {"repo": "omnimarket", "merged_at": "2026-10-16T20:00:00Z"},
    },
}


@dataclass
class FakeProbe:
    os_name: str = "Darwin"
    files: dict[str, str] = field(default_factory=dict)
    commands: dict[tuple[str, ...], str] = field(default_factory=dict)
    trees: dict[str, int] = field(default_factory=dict)
    disks: dict[str, int] = field(default_factory=dict)

    def system(self) -> str:
        return self.os_name

    def read_text(self, path: str) -> str:
        if path not in self.files:
            raise ProbeError(f"{path}: No such file or directory")
        return self.files[path]

    def run(self, argv: tuple[str, ...]) -> str:
        if argv not in self.commands:
            raise ProbeError(f"{argv[0]}: not found")
        return self.commands[argv]

    def disk_free_bytes(self, path: str) -> int:
        if path not in self.disks:
            raise ProbeError(f"{path}: No such file or directory")
        return self.disks[path]

    def tree_bytes(self, path: str) -> int:
        if path not in self.trees:
            raise ProbeError(f"{path}: Permission denied")
        return self.trees[path]


def _darwin() -> FakeProbe:
    return FakeProbe(
        files={"/state.json": json.dumps(STATE)},
        commands={
            ("vm_stat",): VM_STAT,
            (
                "sysctl",
                "-n",
                "vm.swapusage",
            ): "total = 3072.00M  used = 1499.62M  free = 1572.38M",
            ("sysctl", "-n", "kern.memorystatus_level"): "42\n",
            ("launchctl", "list"): LAUNCHCTL,
            (
                "mount",
            ): "/dev/disk3s1 on / (apfs, sealed)\n/dev/disk3s5 on /vol/Data (apfs, local)\n",
        },
        trees={"/vol/Data/.fseventsd": int(19.7 * 1024**3)},
        disks={"/": 120 * 1024**3},
    )


def _request() -> ModelLabSliCollectRequest:
    return ModelLabSliCollectRequest(
        interval_start=NOW,
        now=NOW,
        collector_host="h101",
        pr_watcher_state_path="/state.json",
        disk_paths=("/",),
    )


def test_darwin_sources_become_readings_and_the_night_breaches_show() -> None:
    batch = HandlerLabSliCollect(probe=_darwin()).handle(_request())
    assert batch.source_errors == ()
    readings = {r.sli_id: r for r in batch.readings}
    oldest = readings["oldest_green_unlanded_minutes"].subjects
    # Worst per repo, the draft excluded: omnimarket#4004 green since 00:31Z.
    assert [(s.subject, s.value) for s in oldest] == [
        ("omnimarket#4004", 149.0),
        ("omnibase_infra#9", 60.0),
    ]
    merges = {s.subject: s.value for s in readings["merges_per_hour_per_repo"].subjects}
    assert merges == {"omnibase_infra": 0.0, "omnimarket": 0.25}
    free = readings["host_free_memory_gb"].subjects[0].value
    assert free == pytest.approx(3198576 * 16384 / 1024**3, abs=0.01)
    assert readings["host_swap_used_gb"].subjects[0].value == pytest.approx(
        1.464, abs=0.01
    )
    assert readings["host_memory_pressure_psi"].subjects[0].value == 58
    assert readings["host_fseventsd_gb"].subjects[0].value == pytest.approx(19.7)
    jobs = readings["scheduled_job_failures"]
    assert jobs.subjects[0].value == 2
    assert (
        jobs.detail
        == "nonzero last exit: ai.omninode.host-drift=3, ai.omninode.host-reconcile=78"
    )

    verdicts = {
        e.sli_id: e.status for e in HandlerLabSliEvaluate().handle(batch).evaluations
    }
    assert verdicts["oldest_green_unlanded_minutes"] is EnumLabSliStatus.BREACH
    assert verdicts["merges_per_hour_per_repo"] is EnumLabSliStatus.BREACH
    assert verdicts["host_fseventsd_gb"] is EnumLabSliStatus.BREACH
    assert verdicts["scheduled_job_failures"] is EnumLabSliStatus.BREACH
    assert verdicts["host_disk_free_gb"] is EnumLabSliStatus.OK


def test_linux_sources_become_readings() -> None:
    probe = FakeProbe(
        os_name="Linux",
        files={
            "/proc/meminfo": "MemAvailable:   33554432 kB\nSwapTotal:  8388608 kB\nSwapFree:  4194304 kB\n",
            "/proc/pressure/memory": "some avg10=1.00 avg60=25.50 avg300=3.00 total=1\nfull avg10=0 avg60=0 avg300=0 total=0\n",
        },
        commands={
            (
                "systemctl",
                "--failed",
                "--plain",
                "--no-legend",
            ): "lab-fill.service loaded failed failed Lab fill\n"
        },
    )
    batch = HandlerLabSliCollect(probe=probe).handle(
        ModelLabSliCollectRequest(interval_start=NOW, now=NOW, collector_host="h201")
    )
    readings = {r.sli_id: r for r in batch.readings}
    assert readings["host_free_memory_gb"].subjects[0].value == 32
    assert readings["host_swap_used_gb"].subjects[0].value == 4
    assert readings["host_memory_pressure_psi"].subjects[0].value == 25.5
    assert readings["host_fseventsd_gb"].applicable is False
    assert readings["scheduled_job_failures"].detail == "failed units: lab-fill.service"


def test_an_unreadable_source_is_listed_and_its_indicators_read_unknown() -> None:
    probe = _darwin()
    probe.files.clear()
    probe.trees.clear()
    del probe.commands[("launchctl", "list")]
    batch = HandlerLabSliCollect(probe=probe).handle(_request())
    assert batch.source_errors == (
        "pr-watcher-state: /state.json: No such file or directory",
        "fseventsd: /vol/Data/.fseventsd: Permission denied; /.fseventsd: Permission denied",
        "launchctl: launchctl: not found",
    )
    verdicts = {
        e.sli_id: e.status for e in HandlerLabSliEvaluate().handle(batch).evaluations
    }
    for sli_id in (
        "oldest_green_unlanded_minutes",
        "merges_per_hour_per_repo",
        "host_fseventsd_gb",
        "scheduled_job_failures",
    ):
        assert verdicts[sli_id] is EnumLabSliStatus.UNKNOWN, sli_id
    assert verdicts["host_free_memory_gb"] is EnumLabSliStatus.OK


def test_an_unknown_operating_system_is_a_source_error_not_a_healthy_host() -> None:
    batch = HandlerLabSliCollect(probe=FakeProbe(os_name="Plan9")).handle(
        ModelLabSliCollectRequest(interval_start=NOW, now=NOW, collector_host="hx")
    )
    assert batch.readings == ()
    assert batch.source_errors == ("host-probe: no reader for Plan9",)


def test_a_launchd_label_counts_only_inside_a_whole_label_domain() -> None:
    from omnimarket.nodes.node_lab_sli_collect_effect.handlers.handler_lab_sli_collect import (
        _launchd_failures,
    )

    listing = (
        "PID\tStatus\tLabel\n"
        "-\t78\tcom.onex.vpn-autoconnect\n"
        "-\t5\tcom.onex\n"
        "-\t1\tcom.onexample.unrelated\n"
        "-\t2\tai.omninodex.unrelated\n"
    )
    assert _launchd_failures(listing, ("ai.omninode", "com.onex")) == [
        "com.onex.vpn-autoconnect=78",
        "com.onex=5",
    ]
