# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live profile's receipts feeder: the launching hosts' lane receipts, gathered where the projection reads them (OMN-23427).

The lane runner writes a receipt per lane run on the host that launched it; this adapter gathers the fresh ones from the
configured source directories into the projection's receipts directory, laid out so the projection's own receipts reader
finds them. Every path, glob and field name is ``node_work_state_projection_effect``'s contract ``runtime.receipts``
section -- none is copied here.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.work_state_feeder import ModelFeederOutcome
from omnimarket.models.work_state_feeder.model_receipts_feed_rules import (
    ModelReceiptsFeedRules,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.work_state_source_mapping import (
    receipts_from_documents,
)
from omnimarket.nodes.node_work_state_projection_effect.runtime_work_state import (
    runtime_section,
)

RULES_OVERLAY_ID = "node_work_state_receipts_feeder_scheduled_effect_rules"
RULES_FILE_ENV = "ONEX_WORK_STATE_RECEIPTS_FEED_RULES"


class LaneReceiptsFeeder:
    """Gathers fresh lane receipts from the rules' source directories into the projection's receipts directory.

    One ``feed`` reads every source directory that exists (a missing one is noted in the outcome's cause, the others
    are still read), keeps the receipts the projection's receipts mapping accepts, and writes each one atomically
    under the gathered subdirectory with a path the projection's receipts glob matches. All source directories
    missing is a failed feed; receipts that exist but none newer than the bound is a feed that was not made.
    """

    def __init__(
        self,
        rules: ModelReceiptsFeedRules,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._rules = rules
        self._environ = environ

    @classmethod
    def from_environ(
        cls, environ: Mapping[str, str] | None = None
    ) -> LaneReceiptsFeeder:
        env = os.environ if environ is None else environ
        return cls(
            load_structured_overlay(
                RULES_OVERLAY_ID,
                ModelReceiptsFeedRules,
                env,
                file_env=RULES_FILE_ENV,
            ),
            env,
        )

    def _target(self, env: Mapping[str, str]) -> Path:
        """The projection's receipts directory, resolved exactly as the projection's reader resolves it."""
        rt = runtime_section()["receipts"]
        value = env.get(rt["dir_env"], "").strip()
        return Path(value) if value else Path.home() / rt["dir_fallback"]

    def feed(self, now: datetime) -> ModelFeederOutcome:
        env = os.environ if self._environ is None else self._environ
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        rt = runtime_section()["receipts"]
        glob = rt["glob"]
        glob_parts = len(PurePosixPath(glob).parts)

        missing: list[str] = []
        notes: list[str] = []
        candidates: list[tuple[datetime, dict[str, Any], str]] = []
        for raw_dir in self._rules.source_dirs:
            source = Path(raw_dir)
            if not source.is_dir():
                missing.append(f"{raw_dir}: source directory absent")
                continue
            for path in sorted(source.glob(self._rules.source_glob)):
                if not path.is_file():
                    continue
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
                if (now - mtime).total_seconds() > self._rules.max_receipt_age_s:
                    continue
                try:
                    doc = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    notes.append(f"{path}: unreadable")
                    continue
                if not isinstance(doc, dict) or not receipts_from_documents([doc]):
                    notes.append(f"{path.name}: not a lane receipt")
                    continue
                candidates.append((mtime, doc, path.stem))

        if len(missing) == len(self._rules.source_dirs):
            # Every configured source is gone; nothing was read, so the feed failed outright.
            return ModelFeederOutcome(
                status="failed",
                item_count=0,
                cause="no receipt source directory is readable",
            )
        notes = [*missing, *notes]
        if not candidates:
            return ModelFeederOutcome(
                status="not_fed",
                item_count=0,
                cause=self._cause("no receipt newer than the bound", notes),
            )

        target = self._target(env)
        gathered = target / self._rules.gathered_subdir
        gathered.mkdir(parents=True, exist_ok=True)
        newest: datetime | None = None
        written = 0
        for mtime, doc, stem in sorted(candidates, key=lambda item: item[0]):
            rel = f"{self._rules.gathered_subdir}/{doc.get('lane')}--{stem}.json"
            # The projection's reader globs the receipts directory itself: a laid-out path its glob
            # cannot match would be gathered into a directory the projection never reads.
            rel_path = PurePosixPath(rel)
            if len(rel_path.parts) != glob_parts or not rel_path.match(glob):
                notes.append(f"{rel}: outside the projection's receipts glob")
                continue
            self._write_atomic(gathered / f"{doc.get('lane')}--{stem}.json", doc)
            written += 1
            if newest is None or mtime > newest:
                newest = mtime
        if written == 0 or newest is None:
            return ModelFeederOutcome(
                status="not_fed",
                item_count=0,
                cause=self._cause("no receipt newer than the bound", notes),
            )
        return ModelFeederOutcome(
            status="fed",
            source_as_of=newest,
            item_count=written,
            cause=self._cause("", notes),
        )

    @staticmethod
    def _cause(base: str, notes: list[str]) -> str:
        parts = [base, *notes] if base else list(notes)
        return "; ".join(parts)

    @staticmethod
    def _write_atomic(dest: Path, doc: dict[str, Any]) -> None:
        """Write one receipt whole: its own temp file in the destination's directory, then replace."""
        fd, tmp_name = tempfile.mkstemp(
            dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(doc, sort_keys=True) + "\n")
            Path(tmp_name).replace(dest)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
