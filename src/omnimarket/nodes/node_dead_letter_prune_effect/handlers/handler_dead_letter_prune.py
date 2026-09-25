# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Archive dead-letter rows older than the retention window, verify, then prune (OMN-17001).

Per (topic, partition, UTC day) strictly before the cutoff day:

1. Read the day in offset order, ``max_rows_per_object`` rows per window.
2. For each window, reuse a manifest already in the sink if its object still
   verifies; otherwise encode JSON Lines, gzip deterministically, encrypt with
   the injected cipher, and put the object and its manifest.
3. Verify every window by reading the object back from the sink: object
   checksum, decrypt, plaintext checksum, record count, and the exact set of
   offsets it holds.
4. Only when every window of the day verified: re-read the source offsets of
   each window and require them to equal the archived set, then delete exactly
   those keys in committed batches, and require every batch to delete what it
   named.

A failure anywhere in a day leaves that day's rows in place. A rerun after a
crash between archive and prune finds the verified manifests and prunes without
writing again. Dry run lists eligible days and counts and touches nothing else.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.nodes.node_dead_letter_prune_effect.models import (
    EnumDeadLetterDayStatus,
    EnumDeadLetterPruneVerdict,
    ModelDeadLetterDay,
    ModelDeadLetterDayResult,
    ModelDeadLetterPruneConfig,
    ModelDeadLetterPruneRequest,
    ModelDeadLetterPruneResult,
    ModelDeadLetterRow,
    ModelDeadLetterWindowManifest,
)
from omnimarket.nodes.node_dead_letter_prune_effect.protocols import (
    ProtocolDeadLetterStore,
)
from omnimarket.topic_archive.codec import gzip_deterministic, sha256_hex
from omnimarket.topic_archive.live import LocalDirArchiveSink, NoArchiveCipher
from omnimarket.topic_archive.models import EnumArchiveEncryption
from omnimarket.topic_archive.protocols import (
    ProtocolArchiveCipher,
    ProtocolArchiveSink,
)

_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_SUFFIX = {
    EnumArchiveEncryption.NONE: "",
    EnumArchiveEncryption.AGE_X25519: ".age",
    EnumArchiveEncryption.KMS_ENVELOPE_AES256GCM: ".kmsenv",
}


def contract_config() -> ModelDeadLetterPruneConfig:
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return ModelDeadLetterPruneConfig.model_validate(
        data["config"]["dead_letter_prune"]
    )


def row_line(row: ModelDeadLetterRow) -> bytes:
    return (
        json.dumps(row.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


class _WindowFailedError(Exception):
    pass


class HandlerDeadLetterPrune:
    """Archive-then-prune through an injected store, sink and cipher."""

    def __init__(
        self,
        *,
        store: ProtocolDeadLetterStore | None = None,
        sink: ProtocolArchiveSink | None = None,
        cipher: ProtocolArchiveCipher | None = None,
        now: dt.datetime | None = None,
        config: ModelDeadLetterPruneConfig | None = None,
        max_rows_per_object: int | None = None,
        delete_batch_size: int | None = None,
    ) -> None:
        cfg = config or contract_config()
        self._cfg = cfg
        if store is None or sink is None or cipher is None:
            # Runtime dispatch constructs the handler with no arguments. The
            # default binding is the contract's: event_ledger through the DSN in
            # dsn_env, and the local_dir sink. Both fail fast on a missing env
            # var here rather than falling back to a guessed location.
            if store is None:
                from omnimarket.nodes.node_dead_letter_prune_effect.handlers.postgres_dead_letter_store import (
                    PostgresDeadLetterStore,
                )

                store = PostgresDeadLetterStore(os.environ[cfg.dsn_env])
            sink = sink or LocalDirArchiveSink(Path(os.environ[cfg.local_dir_env]))
            cipher = cipher or NoArchiveCipher()
        self._store: ProtocolDeadLetterStore = store
        self._sink: ProtocolArchiveSink = sink
        self._cipher: ProtocolArchiveCipher = cipher
        self._now = now
        self._max_rows = max_rows_per_object or cfg.max_rows_per_object
        self._batch = delete_batch_size or cfg.delete_batch_size
        # OMN-19657: in-process throttle for the runtime-tick-driven schedule,
        # the same idiom node_github_pr_poller_effect uses for its per-repo
        # `_last_polled`. There is one table here, so one timestamp.
        self._last_scheduled_run: dt.datetime | None = None

    # -- entry ---------------------------------------------------------------

    def handle(
        self, request: ModelDeadLetterPruneRequest | ModelRuntimeTick
    ) -> ModelDeadLetterPruneResult:
        if isinstance(request, ModelRuntimeTick):
            gated = self._gate_scheduled_run(request)
            if gated is None:
                now = (self._now or request.now).astimezone(dt.UTC)
                retention = self._cfg.retention_days
                return ModelDeadLetterPruneResult(
                    verdict=EnumDeadLetterPruneVerdict.SKIPPED_INTERVAL_NOT_ELAPSED,
                    cutoff_day=now.date() - dt.timedelta(days=retention),
                    sink_location=self._sink.location,
                    detail=(
                        "schedule.run_interval_seconds "
                        f"({self._cfg.schedule.run_interval_seconds}s) has not "
                        "elapsed since the last scheduled run"
                    ),
                )
            request = gated
        as_of = (request.as_of or self._now or dt.datetime.now(dt.UTC)).astimezone(
            dt.UTC
        )
        retention = request.retention_days or self._cfg.retention_days
        cutoff = as_of.date() - dt.timedelta(days=retention)
        base: dict[str, Any] = {
            "cutoff_day": cutoff,
            "sink_location": self._sink.location,
        }
        if self._sink.requires_encryption and (
            self._cipher.encryption == EnumArchiveEncryption.NONE
        ):
            return ModelDeadLetterPruneResult(
                verdict=EnumDeadLetterPruneVerdict.REFUSED,
                detail=f"{self._sink.location} leaves the host and the cipher is plaintext",
                **base,
            )
        days = self._store.list_days(cutoff=cutoff, topic_like=self._cfg.topic_like)
        if request.max_days is not None:
            days = days[: request.max_days]
        if not days:
            return ModelDeadLetterPruneResult(
                verdict=EnumDeadLetterPruneVerdict.NOTHING_TO_PRUNE, **base
            )
        if request.dry_run:
            return ModelDeadLetterPruneResult(
                verdict=EnumDeadLetterPruneVerdict.DRY_RUN,
                days=[
                    ModelDeadLetterDayResult(
                        topic=d.topic,
                        partition=d.partition,
                        day=d.day,
                        rows_eligible=d.row_count,
                        status=EnumDeadLetterDayStatus.DRY_RUN,
                    )
                    for d in days
                ],
                **base,
            )
        results = [self._day(d) for d in days]
        ok = all(r.status == EnumDeadLetterDayStatus.PRUNED for r in results)
        return ModelDeadLetterPruneResult(
            verdict=EnumDeadLetterPruneVerdict.PRUNED
            if ok
            else EnumDeadLetterPruneVerdict.FAILED,
            days=results,
            **base,
        )

    # -- schedule --------------------------------------------------------------

    def _gate_scheduled_run(
        self, tick: ModelRuntimeTick
    ) -> ModelDeadLetterPruneRequest | None:
        """Return a request to run now, or None to skip this tick.

        OMN-19657: ticks arrive far more often than
        ``config.dead_letter_prune.schedule.run_interval_seconds``. This gate,
        not the tick subscription, is what makes the schedule daily rather
        than per-tick -- the same in-process elapsed-time idiom
        ``node_github_pr_poller_effect`` uses for ``poll_interval_seconds``.
        Updates ``self._last_scheduled_run`` only when the run proceeds, so a
        skipped tick never resets the interval.
        """
        now = (self._now or tick.now).astimezone(dt.UTC)
        interval = dt.timedelta(seconds=self._cfg.schedule.run_interval_seconds)
        last = self._last_scheduled_run
        if last is not None and (now - last) < interval:
            return None
        self._last_scheduled_run = now
        # tick.now (or the injected self._now override in tests) is the
        # authoritative wall-clock, per ModelRuntimeTick's own contract --
        # threaded through explicitly rather than left to a second
        # dt.datetime.now(UTC) call inside handle(), which would make the
        # retention cutoff nondeterministic against the tick that triggered it.
        return ModelDeadLetterPruneRequest(
            as_of=now, dry_run=self._cfg.schedule.dry_run
        )

    # -- one day -------------------------------------------------------------

    def _day(self, d: ModelDeadLetterDay) -> ModelDeadLetterDayResult:
        written: list[str] = []
        reused: list[str] = []
        verified: list[tuple[ModelDeadLetterWindowManifest, list[int]]] = []
        archived = pruned = 0

        def result(
            status: EnumDeadLetterDayStatus, detail: str = ""
        ) -> ModelDeadLetterDayResult:
            return ModelDeadLetterDayResult(
                topic=d.topic,
                partition=d.partition,
                day=d.day,
                rows_eligible=d.row_count,
                rows_archived=archived,
                rows_pruned=pruned,
                manifests=written,
                reused_manifests=reused,
                status=status,
                detail=detail,
            )

        try:
            after = -1
            while True:
                rows = self._store.read_chunk(
                    topic=d.topic,
                    partition=d.partition,
                    day=d.day,
                    after_offset=after,
                    limit=self._max_rows,
                )
                if not rows:
                    break
                manifest, offsets, was_reused = self._archive_window(d, rows)
                (reused if was_reused else written).append(manifest.manifest_name)
                archived += manifest.record_count
                verified.append((manifest, offsets))
                after = rows[-1].kafka_offset
        except _WindowFailedError as exc:
            return result(EnumDeadLetterDayStatus.FAILED, str(exc))

        # Every window of the day verified. Prune window by window.
        try:
            for manifest, offsets in verified:
                source = self._store.window_offsets(
                    topic=d.topic,
                    partition=d.partition,
                    day=d.day,
                    first_offset=manifest.first_offset,
                    last_offset=manifest.last_offset,
                )
                if source != offsets:
                    return result(
                        EnumDeadLetterDayStatus.FAILED,
                        f"{manifest.object_name}: source window holds {len(source)} offsets, "
                        f"archive holds {len(offsets)}; not deleted",
                    )
                for i in range(0, len(offsets), self._batch):
                    batch = offsets[i : i + self._batch]
                    n = self._store.delete_offsets(
                        topic=d.topic, partition=d.partition, offsets=batch
                    )
                    pruned += n
                    if n != len(batch):
                        return result(
                            EnumDeadLetterDayStatus.FAILED,
                            f"{manifest.object_name}: deleted {n} of {len(batch)} in a batch",
                        )
        except Exception as exc:  # the store failed mid-prune; rows it kept stay
            return result(EnumDeadLetterDayStatus.FAILED, f"prune failed: {exc}")
        return result(EnumDeadLetterDayStatus.PRUNED)

    # -- one window ----------------------------------------------------------

    def _names(self, d: ModelDeadLetterDay, first: int, last: int) -> tuple[str, str]:
        stem = (
            f"{d.topic}/partition={d.partition}/day={d.day.isoformat()}/"
            f"offsets-{first}-{last}"
        )
        return (
            stem + ".jsonl.gz" + _SUFFIX[self._cipher.encryption],
            stem + ".manifest.json",
        )

    def _archive_window(
        self, d: ModelDeadLetterDay, rows: list[ModelDeadLetterRow]
    ) -> tuple[ModelDeadLetterWindowManifest, list[int], bool]:
        first, last = rows[0].kafka_offset, rows[-1].kafka_offset
        obj_name, man_name = self._names(d, first, last)
        if self._sink.exists(man_name):
            try:
                existing = ModelDeadLetterWindowManifest.model_validate_json(
                    self._sink.get(man_name)
                )
                offsets = self._verify(existing)
            except (_WindowFailedError, ValueError):
                # An older schema, a torn write or a failed readback: the
                # window is archived again below and verified before any delete.
                pass
            else:
                if offsets == [r.kafka_offset for r in rows]:
                    return existing, offsets, True
        plaintext = b"".join(row_line(r) for r in rows)
        obj = self._cipher.encrypt(gzip_deterministic(plaintext))
        manifest = ModelDeadLetterWindowManifest(
            source_table=self._cfg.table,
            topic=d.topic,
            partition=d.partition,
            day=d.day,
            first_offset=first,
            last_offset=last,
            record_count=len(rows),
            plaintext_sha256=sha256_hex(plaintext),
            object_sha256=sha256_hex(obj),
            object_bytes=len(obj),
            object_name=obj_name,
            manifest_name=man_name,
            encryption=self._cipher.encryption,
            recipient=self._cipher.recipient,
            archived_at=self._now or dt.datetime.now(dt.UTC),
        )
        try:
            self._sink.put(obj_name, obj)
            self._sink.put(man_name, manifest.model_dump_json(indent=1).encode("utf-8"))
        except Exception as exc:
            raise _WindowFailedError(f"{obj_name}: write failed: {exc}") from exc
        offsets = self._verify(manifest)
        if offsets != [r.kafka_offset for r in rows]:
            raise _WindowFailedError(
                f"{obj_name}: read back offsets differ from the rows written"
            )
        return manifest, offsets, False

    def _verify(self, m: ModelDeadLetterWindowManifest) -> list[int]:
        """Read the object back from the sink; return the offsets it holds."""
        try:
            stored = ModelDeadLetterWindowManifest.model_validate_json(
                self._sink.get(m.manifest_name)
            )
            obj = self._sink.get(m.object_name)
            if stored != m:
                raise _WindowFailedError(f"{m.manifest_name}: stored manifest differs")
            if (
                hashlib.sha256(obj).hexdigest() != m.object_sha256
                or len(obj) != m.object_bytes
            ):
                raise _WindowFailedError(f"{m.object_name}: object checksum mismatch")
            plain = gzip.decompress(self._cipher.decrypt(obj))
        except _WindowFailedError:
            raise
        except Exception as exc:
            raise _WindowFailedError(
                f"{m.object_name}: readback failed: {exc}"
            ) from exc
        if sha256_hex(plain) != m.plaintext_sha256:
            raise _WindowFailedError(f"{m.object_name}: plaintext checksum mismatch")
        offsets: list[int] = []
        for line in plain.splitlines():
            if not line:
                continue
            r = ModelDeadLetterRow.model_validate_json(line)
            if (r.topic, r.partition) != (
                m.topic,
                m.partition,
            ) or r.day_basis().date() != m.day:
                raise _WindowFailedError(
                    f"{m.object_name}: row {r.kafka_offset} is outside the window"
                )
            offsets.append(r.kafka_offset)
        if (
            len(offsets) != m.record_count
            or not offsets
            or (
                offsets[0],
                offsets[-1],
            )
            != (m.first_offset, m.last_offset)
        ):
            raise _WindowFailedError(
                f"{m.object_name}: record count or offset range mismatch"
            )
        return offsets
