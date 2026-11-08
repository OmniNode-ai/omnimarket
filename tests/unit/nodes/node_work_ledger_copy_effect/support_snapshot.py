# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory seams of the snapshot job and restore drill, with named injected breaks."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from omnimarket.nodes.node_work_ledger_copy_effect.adapters.adapters_snapshot import (
    MemoryRowSource,
    MemorySnapshotStore,
    SqliteTableRebuilder,
)
from omnimarket.nodes.node_work_ledger_copy_effect.handlers import (
    HandlerWorkLedgerRestoreDrill,
    HandlerWorkLedgerSnapshot,
)
from omnimarket.nodes.node_work_ledger_copy_effect.models import (
    ModelDrillRequest,
    ModelDrillResult,
    ModelLedgerRow,
    ModelSnapshotBundle,
    ModelSnapshotRequest,
)
from omnimarket.nodes.node_work_ledger_copy_effect.protocols import (
    CopyStepError,
    ProtocolLedgerRowSource,
)

SOURCE_HOST = "source.example.invalid"
SOURCE_PORT = 5432
DRILL_HOST = "drill.example.invalid"
NOW = datetime(2026, 11, 7, 14, 0, 0, tzinfo=UTC)
RUN_ID = "20261107T140000Z"


def rows(count: int = 5) -> tuple[ModelLedgerRow, ...]:
    return tuple(
        ModelLedgerRow(seq=n, row_id=f"id-{n}", text=f"row {n} | text")
        for n in range(1, count + 1)
    )


def snapshot_request(run_id: str = RUN_ID) -> ModelSnapshotRequest:
    return ModelSnapshotRequest(run_id=run_id, started_at=NOW)


def drill_request(run_id: str | None = RUN_ID) -> ModelDrillRequest:
    return ModelDrillRequest(run_id=run_id, started_at=NOW)


class FakeProbe:
    def __init__(self, *, reachable: bool = False, unknowable: bool = False) -> None:
        self.reachable_answer = reachable
        self.unknowable = unknowable
        self.asked: list[tuple[str, int]] = []

    def reachable(self, host: str, port: int) -> bool:
        self.asked.append((host, port))
        if self.unknowable:
            raise CopyStepError(
                "probe", "the probe exited 127, neither reachable nor unreachable"
            )
        return self.reachable_answer


class FakeDrillReporter:
    def __init__(self) -> None:
        self.reports: list[ModelDrillResult] = []

    def report(self, result: ModelDrillResult) -> None:
        self.reports.append(result)


class DroppingStore(MemorySnapshotStore):
    """Injected break ``store_drops_a_row``: storage loses a projection row on the way."""

    def write(self, bundle: ModelSnapshotBundle) -> None:
        super().write(
            bundle.model_copy(update={"projection_rows": bundle.projection_rows[:-1]})
        )


def snapshot_handler(
    store: MemorySnapshotStore,
    *,
    topic: Sequence[ModelLedgerRow] | None = None,
    projection: Sequence[ModelLedgerRow] | None = None,
    store_host: str = DRILL_HOST,
    local_host: str | None = None,
    topic_source: ProtocolLedgerRowSource | None = None,
) -> HandlerWorkLedgerSnapshot:
    return HandlerWorkLedgerSnapshot(
        source_host=SOURCE_HOST,
        store_host=store_host,
        local_host=local_host or store_host,
        topic=topic_source or MemoryRowSource(rows() if topic is None else topic),
        projection=MemoryRowSource(rows() if projection is None else projection),
        store=store,
        keep=3,
    )


def drill_handler(
    store: MemorySnapshotStore,
    *,
    probe: FakeProbe | None = None,
    reporter: FakeDrillReporter | None = None,
    drill_host: str = DRILL_HOST,
    local_host: str | None = None,
) -> HandlerWorkLedgerRestoreDrill:
    return HandlerWorkLedgerRestoreDrill(
        drill_host=drill_host,
        local_host=local_host or drill_host,
        source_host=SOURCE_HOST,
        source_port=SOURCE_PORT,
        store=store,
        probe=probe or FakeProbe(),
        rebuilder=SqliteTableRebuilder("drill_rows"),
        reporter=reporter or FakeDrillReporter(),
    )


def written_store() -> MemorySnapshotStore:
    store = MemorySnapshotStore()
    assert snapshot_handler(store).handle(snapshot_request()).ok
    return store


class RecordingRunner:
    """Records every argument vector it is asked to run and answers from a table, never running one."""

    def __init__(self, answers: Mapping[str, tuple[int, str]] | None = None) -> None:
        self.commands: list[tuple[str, ...]] = []
        self._answers = dict(answers or {})

    def __call__(
        self, argv: Sequence[str], env: Mapping[str, str] | None, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(tuple(argv))
        code, out = self._answers.get(argv[0], (0, ""))
        return subprocess.CompletedProcess(list(argv), code, out, "")


_MUTATING = (
    "stop", "restart", "kill", "rm", "reboot", "shutdown", "poweroff", "systemctl",
    "docker", "rpk", "kubectl", "launchctl", "pkill", "killall", "mv", "dd",
)  # fmt: skip


def assert_no_mutation(
    commands: Sequence[Sequence[str]], source_host: str, allowed: frozenset[str]
) -> None:
    """Every command is on the allow list; none names a mutating verb; the only one that names the
    source host is a connection probe."""
    for argv in commands:
        assert argv[0] in allowed, f"command {argv!r} is not on the allow list"
        assert not set(argv) & set(_MUTATING), f"command {argv!r} names a mutating verb"
        # The ledger append's last argument is the row text, a payload, not a target.
        target = argv[:-1] if argv[0] == "onex-ledger" else argv
        if any(source_host in part for part in target):
            assert argv[0] == "nc", f"{argv!r} touches {source_host}"
            assert "-z" in argv, f"{argv!r} is not a connection probe"
