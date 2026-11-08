# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The snapshot job and the restore drill of the work ledger (OMN-22916, plan section 4.3 step 7).

The job reads the accepted-row topic and the projection, seals their per-row digests into a manifest,
writes both to lab storage on a host other than the source host and reads the snapshot back.

The drill never reads the source. It refuses unless this host is not the source host and the source host
cannot be reached from it, then reads the newest snapshot off lab storage, refuses it if any row differs
from the digest the manifest sealed (a damaged, missing or extra row), rebuilds an empty table from the
accepted-row rows alone and compares it with the projection rows, row for row and sequence for sequence.
"""

from __future__ import annotations

from omnimarket.nodes.node_work_ledger_copy_effect.models import (
    EnumDrillOutcome,
    EnumSnapshotOutcome,
    ModelDrillRequest,
    ModelDrillResult,
    ModelLedgerRow,
    ModelSnapshotBundle,
    ModelSnapshotManifest,
    ModelSnapshotRequest,
    ModelSnapshotResult,
)
from omnimarket.nodes.node_work_ledger_copy_effect.protocols import (
    CopyStepError,
    ProtocolDrillReporter,
    ProtocolLedgerRowSource,
    ProtocolSnapshotStore,
    ProtocolSourceProbe,
    ProtocolTableRebuilder,
)


def find_damaged_rows(bundle: ModelSnapshotBundle) -> tuple[str, ...]:
    """Name every place the snapshot differs from what its manifest sealed; empty means intact."""
    manifest = bundle.manifest
    damaged: list[str] = []
    if not manifest.sealed_ok():
        damaged.append("manifest: its own digest does not match its row digests")
    for label, rows, sealed in (
        ("topic", bundle.topic_rows, manifest.topic_digests),
        ("projection", bundle.projection_rows, manifest.projection_digests),
    ):
        if len(rows) != len(sealed):
            damaged.append(f"{label}: {len(rows)} rows held, {len(sealed)} sealed")
        for index, (row, digest) in enumerate(zip(rows, sealed, strict=False)):
            if row.digest != digest:
                damaged.append(
                    f"{label}: row {index} (seq {row.seq}) differs from its seal"
                )
    return tuple(damaged)


def _compare(
    rebuilt: tuple[ModelLedgerRow, ...], projection: tuple[ModelLedgerRow, ...]
) -> tuple[str, ...]:
    problems: list[str] = []
    if len(rebuilt) != len(projection):
        problems.append(
            f"rebuilt {len(rebuilt)} rows, projection holds {len(projection)}"
        )
    for index, (built, held) in enumerate(zip(rebuilt, projection, strict=False)):
        if built != held:
            problems.append(
                f"row {index}: rebuilt seq {built.seq}, projection seq {held.seq}"
            )
            break
    rebuilt_seqs = {row.seq for row in rebuilt}
    extra = sorted(row.seq for row in projection if row.seq not in rebuilt_seqs)
    if extra:
        problems.append(f"in the projection and not on the topic: seq {extra[:5]}")
    return tuple(problems)


class HandlerWorkLedgerSnapshot:
    def __init__(
        self,
        *,
        source_host: str,
        store_host: str,
        topic: ProtocolLedgerRowSource,
        projection: ProtocolLedgerRowSource,
        store: ProtocolSnapshotStore,
        keep: int,
    ) -> None:
        self._source_host = source_host
        self._store_host = store_host
        self._topic = topic
        self._projection = projection
        self._store = store
        self._keep = keep

    def handle(self, request: ModelSnapshotRequest) -> ModelSnapshotResult:
        run_id = request.run_id
        if self._store_host == self._source_host:
            return ModelSnapshotResult(
                run_id=run_id,
                outcome=EnumSnapshotOutcome.REFUSED_SAME_HOST,
                detail=f"lab storage is on the source host {self._source_host}",
            )
        try:
            topic = self._topic.rows()
            projection = self._projection.rows()
            topic_again = self._topic.rows()
        except CopyStepError as exc:
            return ModelSnapshotResult(
                run_id=run_id,
                outcome=EnumSnapshotOutcome.SOURCE_FAILED,
                detail=str(exc),
            )
        if topic_again != topic:
            return ModelSnapshotResult(
                run_id=run_id,
                outcome=EnumSnapshotOutcome.SOURCE_FAILED,
                detail="the accepted-row topic changed while the projection was exported",
            )
        if not topic or not projection:
            return ModelSnapshotResult(
                run_id=run_id,
                outcome=EnumSnapshotOutcome.REFUSED_EMPTY,
                detail=f"topic holds {len(topic)} rows, projection holds {len(projection)}",
            )
        manifest = ModelSnapshotManifest.seal(
            run_id, request.started_at, self._source_host, topic, projection
        )
        bundle = ModelSnapshotBundle(
            manifest=manifest, topic_rows=topic, projection_rows=projection
        )
        counts = {"topic_rows": len(topic), "projection_rows": len(projection)}
        try:
            self._store.write(bundle)
            held = self._store.read(run_id)
        except CopyStepError as exc:
            return ModelSnapshotResult(
                run_id=run_id,
                outcome=EnumSnapshotOutcome.STORE_FAILED,
                detail=str(exc),
                **counts,
            )
        if held is None or find_damaged_rows(held) or held.manifest != manifest:
            return ModelSnapshotResult(
                run_id=run_id,
                outcome=EnumSnapshotOutcome.READBACK_MISMATCH,
                detail="the snapshot read back from storage differs from what was written",
                **counts,
            )
        warning = ""
        try:
            pruned = self._store.prune(self._keep)
        except CopyStepError as exc:
            pruned, warning = (), f"prune failed: {exc}"
        return ModelSnapshotResult(
            run_id=run_id,
            outcome=EnumSnapshotOutcome.WRITTEN,
            detail=f"{len(topic)} topic rows and {len(projection)} projection rows written and read back",
            manifest_sha256=manifest.manifest_sha256,
            pruned=pruned,
            warning=warning,
            **counts,
        )


class HandlerWorkLedgerRestoreDrill:
    def __init__(
        self,
        *,
        drill_host: str,
        source_host: str,
        source_port: int,
        store: ProtocolSnapshotStore,
        probe: ProtocolSourceProbe,
        rebuilder: ProtocolTableRebuilder,
        reporter: ProtocolDrillReporter,
    ) -> None:
        self._drill_host = drill_host
        self._source_host = source_host
        self._source_port = source_port
        self._store = store
        self._probe = probe
        self._rebuilder = rebuilder
        self._reporter = reporter

    def handle(self, request: ModelDrillRequest) -> ModelDrillResult:
        result = self._run(request)
        self._reporter.report(result)
        return result

    def _result(
        self,
        run_id: str | None,
        outcome: EnumDrillOutcome,
        detail: str,
        **more: object,
    ) -> ModelDrillResult:
        return ModelDrillResult(
            run_id=run_id,
            outcome=outcome,
            detail=detail,
            drill_host=self._drill_host,
            source_host=self._source_host,
            **more,
        )

    def _run(self, request: ModelDrillRequest) -> ModelDrillResult:
        run_id = request.run_id
        if self._drill_host == self._source_host:
            return self._result(
                run_id,
                EnumDrillOutcome.REFUSED_SAME_HOST,
                "the drill runs on a host other than the source host",
            )
        try:
            reachable = self._probe.reachable(self._source_host, self._source_port)
        except CopyStepError as exc:
            return self._result(
                run_id,
                EnumDrillOutcome.REFUSED_PROBE_FAILED,
                f"reachability unknown: {exc}",
            )
        if reachable:
            return self._result(
                run_id,
                EnumDrillOutcome.REFUSED_SOURCE_REACHABLE,
                f"{self._source_host} answers from this host; the drill needs it unreachable",
            )
        try:
            run_id = run_id or self._store.newest()
            bundle = self._store.read(run_id) if run_id else None
        except CopyStepError as exc:
            return self._result(
                run_id,
                EnumDrillOutcome.REFUSED_NO_SNAPSHOT,
                f"snapshot unreadable: {exc}",
            )
        if bundle is None:
            return self._result(
                run_id,
                EnumDrillOutcome.REFUSED_NO_SNAPSHOT,
                "no snapshot on lab storage",
            )
        seal = bundle.manifest.manifest_sha256
        damaged = find_damaged_rows(bundle)
        if bundle.manifest.source_host != self._source_host:
            damaged = (
                f"manifest: source_host {bundle.manifest.source_host!r} is not "
                f"{self._source_host!r}",
                *damaged,
            )
        if run_id and bundle.manifest.run_id != run_id:
            damaged = (
                f"manifest: run_id {bundle.manifest.run_id!r} is not {run_id!r}",
                *damaged,
            )
        if damaged:
            return self._result(
                run_id,
                EnumDrillOutcome.REFUSED_DAMAGED,
                f"{len(damaged)} places differ from the manifest: {damaged[0]}",
                damaged=damaged,
                manifest_sha256=seal,
            )
        try:
            rebuilt = self._rebuilder.rebuild(bundle.topic_rows)
        except CopyStepError as exc:
            return self._result(
                run_id,
                EnumDrillOutcome.REBUILD_MISMATCH,
                f"rebuild failed: {exc}",
                manifest_sha256=seal,
            )
        problems = _compare(rebuilt, bundle.projection_rows)
        if problems:
            return self._result(
                run_id,
                EnumDrillOutcome.REBUILD_MISMATCH,
                "; ".join(problems),
                rebuilt_rows=len(rebuilt),
                manifest_sha256=seal,
            )
        return self._result(
            run_id,
            EnumDrillOutcome.PASSED,
            f"{len(rebuilt)} rows rebuilt from the accepted-row topic equal the projection, row for row",
            rebuilt_rows=len(rebuilt),
            manifest_sha256=seal,
        )
