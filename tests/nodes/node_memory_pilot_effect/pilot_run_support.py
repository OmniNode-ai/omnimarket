# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures of the daily run tests (OMN-23619): a synthetic frozen pilot, fakes for every port."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot import (
    ModelMemoryPilotDocument,
    ModelMemoryPilotOverlay,
)
from omnimarket.nodes.node_memory_pilot_effect.protocols.protocol_memory_pilot import (
    BackendAnswer,
    LabModelUnavailableError,
)

from .conftest import PILOT_DIR

CORR = UUID("00000000-0000-4000-a000-0000000000c1")
NOW = datetime(2026, 11, 11, 3, 0, tzinfo=UTC)
NAMES = {
    "overall_gain": "overall_gain_over_baseline",
    "hard_gain": "hard_gain_over_baseline",
    "planted_recalled": "planted_facts_recalled",
    "abstain_margin": "abstain_margin_over_baseline",
    "ingest_seconds": "ingest_seconds",
    "failed_items": "failed_items",
    "query_p95_seconds": "query_p95_seconds",
    "outside_connections": "outside_connections",
}
#: question text -> (category, pattern the right answer matches)
QUESTIONS = {
    "What colour is the lab canary?": ("lexical", "teal"),
    "Which port does the relay use?": ("lexical", "4821"),
    "Name the owner of the estimator seed.": ("planted", "Quill"),
    "Where is scratch retained?": ("planted", "cold shelf"),
    "Which two ADRs set the queue limit?": ("multihop", "ADR-7"),
    "How many hosts does the pool hold?": ("aggregate", "twelve"),
    "What was decided about the moon base?": ("abstain", ""),
}
DOCS = {
    "adrs/ADR-0001.md": "The lab canary is teal.\n\nThe relay uses port 4821.",
    "doctrine/D-1.md": "The queue limit is set by ADR-7 and ADR-9. The pool holds twelve hosts.",
}
PLANTED = {
    "planted_notes/PN-1.txt": "The estimator seed is owned by Quill.",
    "planted_notes/PN-2.txt": "Scratch is retained on the cold shelf.",
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_frozen_pilot(tmp: Path) -> tuple[Path, Path, str]:
    """A frozen pilot directory, the corpus root, and the frozen hash; the real bar.json carries the prompt."""
    pilot = tmp / "pilot"
    corpus = tmp / "corpus"
    (pilot / "planted_notes").mkdir(parents=True)
    shutil.copy(PILOT_DIR / "bar.json", pilot / "bar.json")
    files = []
    for name, text in DOCS.items():
        target = corpus / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        files.append({"path": name, "sha256": _sha(text.encode()), "bytes": len(text)})
    for name, text in PLANTED.items():
        (pilot / name).write_text(text, encoding="utf-8")
        files.append({"path": name, "sha256": _sha(text.encode()), "bytes": len(text)})
    (pilot / "corpus_manifest.json").write_text(
        json.dumps({"files": files}), encoding="utf-8"
    )
    questions = [
        {
            "id": f"Q{i}",
            "category": category,
            "question": text,
            "all_of": [[pattern]] if pattern else [],
            "gold": pattern,
        }
        for i, (text, (category, pattern)) in enumerate(QUESTIONS.items())
    ]
    (pilot / "questions.json").write_text(
        json.dumps(
            {
                "abstain_regex": "NOT IN CONTEXT",
                "scoring": "literal",
                "questions": questions,
            }
        ),
        encoding="utf-8",
    )
    listing = "".join(
        f"{_sha((pilot / n).read_bytes())}  {n}\n"
        for n in (
            "bar.json",
            "corpus_manifest.json",
            "questions.json",
            *sorted(PLANTED),
        )
    )
    (pilot / "FROZEN.sha256").write_text(listing, encoding="utf-8")
    return pilot, corpus, _sha(listing.encode())


def make_run_overlay(
    tmp: Path, *, parallel: bool = True, allowed: tuple[str, ...] = ("127.0.0.1",)
) -> ModelMemoryPilotOverlay:
    pilot, corpus, frozen = build_frozen_pilot(tmp)
    endpoint = {
        "base_url": "http://127.0.0.1:9/v1",
        "model": "lab",
        "timeout_seconds": 2,
    }
    return ModelMemoryPilotOverlay.model_validate(
        {
            "backend": "cognee",
            "frozen_hash": frozen,
            "pilot_dir": pilot,
            "telemetry_disabled": True,
            "allowed_hosts": allowed,
            "chat": endpoint,
            "embeddings": endpoint,
            "embedding_dimensions": 8,
            "ingest_data_per_batch": 2,
            "storage_root": tmp / "store",
            "run": {
                "corpus_root": corpus,
                "dataset": "d",
                "trials": 2,
                "result_file": tmp / "result.json",
                "event_journal": tmp / "events.jsonl",
                "runs_directory": "runs",
                "parallel_arms": parallel,
                "metric_names": NAMES,
            },
        }
    )


def _right(question: str) -> str:
    _category, pattern = QUESTIONS[question]
    return pattern or "NOT IN CONTEXT"


class FakeAnswerer:
    """The baseline's lab model: right on the lexical questions only (a weaker arm), or down."""

    def __init__(self, *, down: bool = False) -> None:
        self.down = down

    async def answer(self, system_prompt: str, question: str, context: str) -> str:
        if self.down:
            raise LabModelUnavailableError("ConnectError")
        category = QUESTIONS[question][0]
        return _right(question) if category in ("lexical", "abstain") else "unsure"


class FakeCandidate:
    name = "cognee"

    def __init__(
        self, *, good: bool = True, fail_ingest: bool = False, down: bool = False
    ) -> None:
        self.good, self.fail_ingest, self.down = good, fail_ingest, down
        self.store: Path | None = None

    async def ingest(
        self, dataset: str, documents: tuple[ModelMemoryPilotDocument, ...]
    ) -> int:
        if self.down:
            raise LabModelUnavailableError("ConnectError")
        if self.fail_ingest:
            raise RuntimeError("cognify did not complete")
        return len(documents)

    async def query(self, dataset: str, question: str) -> BackendAnswer:
        return BackendAnswer(_right(question) if self.good else "unsure", ())


@dataclass
class FakeSink:
    writes: list[tuple[datetime, dict[str, float]]] = field(default_factory=list)
    fail: bool = False

    def write(self, measured_at: datetime, metrics: dict[str, float]) -> None:
        if self.fail:
            raise OSError("disk full")
        self.writes.append((measured_at, metrics))


class FakeAudit:
    def __init__(self, outside: tuple[str, ...] = ()) -> None:
        self.outside, self.started_with = outside, ()

    def start(self, allowed_hosts: tuple[str, ...]) -> None:
        self.started_with = allowed_hosts

    def stop(self) -> tuple[str, ...]:
        return self.outside


class Ticker:
    """A monotonic clock that moves 1 s a call, so latencies are 1 s."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now
