# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Archive consumer_flow_windows rows older than the retention window, verify,
then prune (OMN-19658).

Per UTC day of window_start strictly before the cutoff day:

1. List the day's projection cursors, then read its rows by cursor,
   ``max_rows_per_object`` rows per window.
2. For each window, reuse a manifest already in the sink if its object still
   verifies; otherwise encode JSON Lines, gzip deterministically, encrypt with
   the injected cipher, and put the object and its manifest.
3. Verify every window by reading the object back from the sink: object
   checksum, decrypt, plaintext checksum, record count, the day of every row,
   and the exact cursors it holds.
4. Only when every window of the day verified: re-read the source rows under
   each window's cursors and require them to encode to exactly the archived
   plaintext (the projection upserts, so a late heartbeat could rewrite an
   archived window), then delete exactly those cursors in committed batches,
   bounded to the day, and require every batch to delete what it named.

A failure anywhere in a day leaves the rest of that day in place. A rerun after
a crash between archive and prune finds the verified manifests and prunes
without writing again. Dry run lists eligible days and counts and touches
nothing else. A day on or after the cutoff is never read, even if a store
lists one.
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

from omnimarket.nodes.node_consumer_flow_prune_effect.models import (
    EnumConsumerFlowDayStatus,
    EnumConsumerFlowPruneVerdict,
    ModelConsumerFlowDay,
    ModelConsumerFlowDayResult,
    ModelConsumerFlowPruneConfig,
    ModelConsumerFlowPruneRequest,
    ModelConsumerFlowPruneResult,
    ModelConsumerFlowRow,
    ModelConsumerFlowWindowManifest,
)
from omnimarket.nodes.node_consumer_flow_prune_effect.protocols import (
    ProtocolConsumerFlowStore,
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


def contract_config() -> ModelConsumerFlowPruneConfig:
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return ModelConsumerFlowPruneConfig.model_validate(
        data["config"]["consumer_flow_prune"]
    )


def encode_rows(rows: list[ModelConsumerFlowRow]) -> bytes:
    """Canonical JSON Lines: the same rows always give the same bytes."""
    return b"".join(
        (
            json.dumps(r.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        for r in rows
    )


class _WindowFailedError(Exception):
    pass


class HandlerConsumerFlowPrune:
    """Archive-then-prune through an injected store, sink and cipher."""

    def __init__(
        self,
        *,
        store: ProtocolConsumerFlowStore | None = None,
        sink: ProtocolArchiveSink | None = None,
        cipher: ProtocolArchiveCipher | None = None,
        now: dt.datetime | None = None,
        config: ModelConsumerFlowPruneConfig | None = None,
        max_rows_per_object: int | None = None,
        delete_batch_size: int | None = None,
    ) -> None:
        cfg = config or contract_config()
        self._cfg = cfg
        if store is None or sink is None or cipher is None:
            # Runtime dispatch constructs the handler with no arguments. The
            # default binding is the contract's: the table through the DSN in
            # dsn_env, and the local_dir sink. Both fail fast on a missing env
            # var here rather than falling back to a guessed location.
            if store is None:
                from omnimarket.nodes.node_consumer_flow_prune_effect.handlers.postgres_consumer_flow_store import (
                    PostgresConsumerFlowStore,
                )

                store = PostgresConsumerFlowStore(os.environ[cfg.dsn_env])
            sink = sink or LocalDirArchiveSink(Path(os.environ[cfg.local_dir_env]))
            cipher = cipher or NoArchiveCipher()
        self._store: ProtocolConsumerFlowStore = store
        self._sink: ProtocolArchiveSink = sink
        self._cipher: ProtocolArchiveCipher = cipher
        self._now = now
        self._max_rows = max_rows_per_object or cfg.max_rows_per_object
        self._batch = delete_batch_size or cfg.delete_batch_size
        # In-process throttle for the runtime-tick schedule, the idiom
        # node_dead_letter_prune_effect uses (OMN-19657): one table, one stamp.
        self._last_scheduled_run: dt.datetime | None = None

    # -- entry ---------------------------------------------------------------

    def handle(
        self, request: ModelConsumerFlowPruneRequest | ModelRuntimeTick
    ) -> ModelConsumerFlowPruneResult:
        if isinstance(request, ModelRuntimeTick):
            gated = self._gate_scheduled_run(request)
            if gated is None:
                now = (self._now or request.now).astimezone(dt.UTC)
                return ModelConsumerFlowPruneResult(
                    verdict=EnumConsumerFlowPruneVerdict.SKIPPED_INTERVAL_NOT_ELAPSED,
                    cutoff_day=now.date() - dt.timedelta(days=self._cfg.retention_days),
                    sink_location=self._sink.location,
                    detail=(
                        f"schedule.run_interval_seconds "
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
            return ModelConsumerFlowPruneResult(
                verdict=EnumConsumerFlowPruneVerdict.REFUSED,
                detail=f"{self._sink.location} leaves the host and the cipher is plaintext",
                **base,
            )
        days = self._store.list_days(cutoff=cutoff)
        if request.max_days is not None:
            days = days[: request.max_days]
        if not days:
            return ModelConsumerFlowPruneResult(
                verdict=EnumConsumerFlowPruneVerdict.NOTHING_TO_PRUNE, **base
            )
        if request.dry_run:
            return ModelConsumerFlowPruneResult(
                verdict=EnumConsumerFlowPruneVerdict.DRY_RUN,
                days=[
                    ModelConsumerFlowDayResult(
                        day=d.day,
                        rows_eligible=d.row_count,
                        status=EnumConsumerFlowDayStatus.DRY_RUN,
                    )
                    for d in days
                ],
                **base,
            )
        results = [self._day(d, cutoff) for d in days]
        ok = all(r.status == EnumConsumerFlowDayStatus.PRUNED for r in results)
        return ModelConsumerFlowPruneResult(
            verdict=EnumConsumerFlowPruneVerdict.PRUNED
            if ok
            else EnumConsumerFlowPruneVerdict.FAILED,
            days=results,
            **base,
        )

    # -- schedule ------------------------------------------------------------

    def _gate_scheduled_run(
        self, tick: ModelRuntimeTick
    ) -> ModelConsumerFlowPruneRequest | None:
        """A request to run now, or None to skip this tick.

        Ticks arrive far more often than the interval. The stamp moves only
        when a run proceeds, so a skipped tick never resets the interval. The
        tick's own clock is the as-of instant, so the cutoff is fixed by the
        tick that triggered the run.
        """
        now = (self._now or tick.now).astimezone(dt.UTC)
        interval = dt.timedelta(seconds=self._cfg.schedule.run_interval_seconds)
        last = self._last_scheduled_run
        if last is not None and (now - last) < interval:
            return None
        self._last_scheduled_run = now
        return ModelConsumerFlowPruneRequest(
            as_of=now, dry_run=self._cfg.schedule.dry_run
        )

    # -- one day -------------------------------------------------------------

    def _day(
        self, d: ModelConsumerFlowDay, cutoff: dt.date
    ) -> ModelConsumerFlowDayResult:
        written: list[str] = []
        reused: list[str] = []
        verified: list[tuple[ModelConsumerFlowWindowManifest, list[int]]] = []
        archived = pruned = 0

        def result(
            status: EnumConsumerFlowDayStatus, detail: str = ""
        ) -> ModelConsumerFlowDayResult:
            return ModelConsumerFlowDayResult(
                day=d.day,
                rows_eligible=d.row_count,
                rows_archived=archived,
                rows_pruned=pruned,
                manifests=written,
                reused_manifests=reused,
                status=status,
                detail=detail,
            )

        if d.day >= cutoff:
            return result(
                EnumConsumerFlowDayStatus.FAILED,
                f"store listed {d.day}, on or after the cutoff day {cutoff}; not read",
            )
        try:
            cursors = self._store.day_cursors(day=d.day)
            for i in range(0, len(cursors), self._max_rows):
                rows = self._store.read_rows(
                    day=d.day, cursors=cursors[i : i + self._max_rows]
                )
                if not rows:
                    continue
                manifest, held, was_reused = self._archive_window(d.day, rows)
                (reused if was_reused else written).append(manifest.manifest_name)
                archived += manifest.record_count
                verified.append((manifest, held))
        except _WindowFailedError as exc:
            return result(EnumConsumerFlowDayStatus.FAILED, str(exc))
        except Exception as exc:  # the store failed mid-read; nothing deleted
            return result(EnumConsumerFlowDayStatus.FAILED, f"archive failed: {exc}")

        # Every window of the day verified. Prune window by window.
        try:
            for manifest, held in verified:
                now = self._store.read_rows(day=d.day, cursors=held)
                if sha256_hex(encode_rows(now)) != manifest.plaintext_sha256:
                    return result(
                        EnumConsumerFlowDayStatus.FAILED,
                        f"{manifest.object_name}: the source rows under its "
                        f"{len(held)} cursors no longer match the archive; not deleted",
                    )
                for j in range(0, len(held), self._batch):
                    batch = held[j : j + self._batch]
                    n = self._store.delete_cursors(day=d.day, cursors=batch)
                    pruned += n
                    if n != len(batch):
                        return result(
                            EnumConsumerFlowDayStatus.FAILED,
                            f"{manifest.object_name}: deleted {n} of {len(batch)} in a batch",
                        )
        except Exception as exc:  # the store failed mid-prune; rows it kept stay
            return result(EnumConsumerFlowDayStatus.FAILED, f"prune failed: {exc}")
        return result(EnumConsumerFlowDayStatus.PRUNED)

    # -- one window ----------------------------------------------------------

    def _names(self, day: dt.date, first: int, last: int) -> tuple[str, str]:
        stem = f"{self._cfg.table}/day={day.isoformat()}/cursors-{first}-{last}"
        return (
            stem + ".jsonl.gz" + _SUFFIX[self._cipher.encryption],
            stem + ".manifest.json",
        )

    def _archive_window(
        self, day: dt.date, rows: list[ModelConsumerFlowRow]
    ) -> tuple[ModelConsumerFlowWindowManifest, list[int], bool]:
        want = [r.projection_cursor for r in rows]
        obj_name, man_name = self._names(day, want[0], want[-1])
        plaintext = encode_rows(rows)
        if self._sink.exists(man_name):
            try:
                existing = ModelConsumerFlowWindowManifest.model_validate_json(
                    self._sink.get(man_name)
                )
                held = self._verify(existing)
            except (_WindowFailedError, ValueError):
                # An older schema, a torn write or a failed readback: the
                # window is archived again below and verified before any delete.
                pass
            else:
                if held == want and existing.plaintext_sha256 == sha256_hex(plaintext):
                    return existing, held, True
        obj = self._cipher.encrypt(gzip_deterministic(plaintext))
        manifest = ModelConsumerFlowWindowManifest(
            source_table=self._cfg.table,
            day=day,
            first_cursor=want[0],
            last_cursor=want[-1],
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
        held = self._verify(manifest)
        if held != want:
            raise _WindowFailedError(
                f"{obj_name}: read back cursors differ from the rows written"
            )
        return manifest, held, False

    def _verify(self, m: ModelConsumerFlowWindowManifest) -> list[int]:
        """Read the object back from the sink; return the cursors it holds."""
        try:
            stored = ModelConsumerFlowWindowManifest.model_validate_json(
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
        held: list[int] = []
        for line in plain.splitlines():
            if not line:
                continue
            r = ModelConsumerFlowRow.model_validate_json(line)
            if r.day() != m.day:
                raise _WindowFailedError(
                    f"{m.object_name}: row {r.projection_cursor} is outside the day"
                )
            held.append(r.projection_cursor)
        if (
            len(held) != m.record_count
            or not held
            or (held[0], held[-1]) != (m.first_cursor, m.last_cursor)
        ):
            raise _WindowFailedError(
                f"{m.object_name}: record count or cursor range mismatch"
            )
        return held
