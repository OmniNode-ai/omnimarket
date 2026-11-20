# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The publish status's cache tally: each step's counts of the cycle, and a red row for a step that keeps reading its source.

``stores=0`` with source reads went unseen for days because nothing read the tally. ``fold_cache_tally`` reads the receipts the cycle's
steps wrote (the same receipts ``tally_receipt_lines`` turns into log lines), counts for each step the cycles in a row in which it stored
nothing and read its source, and names a step at the contract's ``tally_red_after_cycles`` in ``red_rows``. The count is kept in the file
the contract names (``tally_streak_relpath``) and moves once per receipt, so folding the same cycle twice counts it once. A receipt or a
count file that cannot be read never stops the publish: the step counts from zero.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.models.model_cache_tally_status import (
    ModelCacheRedRow,
    ModelCacheStepTally,
    ModelCacheTallyStatus,
)
from omnimarket.models.model_source_read_cache import ModelSourceCacheTally
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader_factory import (
    OWN_CONTRACT,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.tally_receipt_lines import (
    RECEIPT_GLOB,
    RECEIPT_TIME_FORMAT,
)

_RECEIPT_EXTRA_KEYS = ("step", "written_at")


def _runtime(contract_path: Path) -> dict[str, object]:
    runtime = yaml.safe_load(contract_path.read_text(encoding="utf-8"))["runtime"]
    return dict(runtime)


def _utc(moment: datetime) -> datetime:
    return (
        moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    )


def _load_counts(path: Path) -> dict[str, tuple[int, str]]:
    """Each step's count and the ``written_at`` of the receipt that last moved it; empty when the file is absent or unusable."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        return {
            str(step): (int(entry["cycles"]), str(entry["written_at"]))
            for step, entry in document["steps"].items()
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def _save_counts(path: Path, counts: dict[str, tuple[int, str]]) -> None:
    document = {
        "steps": {
            step: {"cycles": cycles, "written_at": written_at}
            for step, (cycles, written_at) in sorted(counts.items())
        }
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        scratch = path.with_name(path.name + ".tmp")
        scratch.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
        scratch.replace(path)
    except OSError:
        return


def fold_cache_tally(
    home: Path, since: datetime, contract_path: Path = OWN_CONTRACT
) -> ModelCacheTallyStatus:
    """The tally of every receipt written at or after ``since`` and the red rows after folding the cycle into each step's count."""
    runtime = _runtime(contract_path)
    directory = home / str(runtime["receipt_dir_relpath"])
    red_after = int(str(runtime["tally_red_after_cycles"]))
    counts_path = home / str(runtime["tally_streak_relpath"])
    if not directory.is_dir():
        return ModelCacheTallyStatus()
    threshold = _utc(since).replace(microsecond=0)
    counts = _load_counts(counts_path)
    steps: list[ModelCacheStepTally] = []
    red: list[ModelCacheRedRow] = []
    for path in sorted(directory.glob(RECEIPT_GLOB), key=lambda receipt: receipt.name):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            step = str(document["step"])
            written = document["written_at"]
            written_at = _utc(datetime.strptime(written, RECEIPT_TIME_FORMAT))
            tally = ModelSourceCacheTally.model_validate(
                {k: v for k, v in document.items() if k not in _RECEIPT_EXTRA_KEYS}
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if written_at < threshold:
            continue
        cycles, last = counts.get(step, (0, ""))
        if written > last:
            cycles = cycles + 1 if tally.stores == 0 and tally.source_reads > 0 else 0
            counts[step] = (cycles, written)
        steps.append(
            ModelCacheStepTally(
                step=step,
                hits=tally.hits,
                misses=tally.misses,
                stores=tally.stores,
                source_reads=tally.source_reads,
                source_seconds=tally.source_seconds,
                no_store_cycles=cycles,
            )
        )
        if cycles >= red_after:
            red.append(
                ModelCacheRedRow(
                    step=step,
                    cycles=cycles,
                    detail=(
                        f"{step} stored nothing and read its source in {cycles} cycles in a row"
                        f" (last cycle: source_reads={tally.source_reads}"
                        f" source_seconds={tally.source_seconds})"
                    ),
                )
            )
    _save_counts(counts_path, counts)
    return ModelCacheTallyStatus(steps=tuple(steps), red_rows=tuple(red))
