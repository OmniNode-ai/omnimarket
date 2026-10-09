# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerShadowReview: one tick of the shadow reviewer experiment (OMN-20422).

handle(ModelShadowReviewRequest) -> ModelShadowReviewResult. Selects the PRs
the pre-registration puts in the sample, reads each diff from the canonical
clone, drops a PR whose diff carries a secret pattern before any call, runs
the reviewer arms, and writes one record per PR to the experiment store.
Shadow only: it never posts, comments, labels or blocks.

Store layout under ``store_root``::

    records/<repo>__<n>.json     one ModelShadowReviewRecord per reviewed PR
    drops/<repo>__<n>.json       a PR dropped by a diff-stage rule, with reason
    artifacts/<repo>__<n>/       diff.patch, the A2 prompt and harness receipts
    ticks.jsonl                  one summary line per tick
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_shadow_review_effect.handlers.shadow_effects import (
    ProtocolShadowDiffSource,
    ProtocolShadowReviewer,
    scan_for_secrets,
)
from omnimarket.nodes.node_shadow_review_effect.handlers.shadow_selection import (
    select_candidates,
)
from omnimarket.nodes.node_shadow_review_effect.models.model_shadow_review import (
    EnumShadowDecision,
    EnumShadowStratum,
    ModelShadowReviewRecord,
    ModelShadowReviewRequest,
    ModelShadowReviewResult,
    ModelShadowSelection,
)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def store_name(key: str) -> str:
    repo, _, number = key.partition("#")
    return f"{repo}__{number}"


class HandlerShadowReview:
    """Definition-B handler; the diff source and reviewer are injected."""

    def __init__(
        self,
        diff_source: ProtocolShadowDiffSource,
        reviewer: ProtocolShadowReviewer,
        clock: Callable[[], str] = _now,
    ):
        self._diff_source = diff_source
        self._reviewer = reviewer
        self._clock = clock

    @staticmethod
    def _read_store(root: Path) -> tuple[frozenset[str], int]:
        seen: set[str] = set()
        public = 0
        for sub in ("records", "drops"):
            folder = root / sub
            if not folder.is_dir():
                continue
            for path in folder.glob("*.json"):
                data = json.loads(path.read_text(encoding="utf-8"))
                seen.add(str(data["key"]))
                if (
                    sub == "records"
                    and data.get("stratum") == EnumShadowStratum.PUBLIC.value
                ):
                    public += 1
        return frozenset(seen), public

    @staticmethod
    def _write(path: Path, payload: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)

    def handle(self, request: ModelShadowReviewRequest) -> ModelShadowReviewResult:
        root = request.store_root
        seen, public_reviewed = self._read_store(root)
        selections = select_candidates(
            request.candidates,
            request.policy,
            already_seen=seen,
            public_reviewed=public_reviewed,
            only=frozenset(request.only),
        )
        by_key = {c.key: c for c in request.candidates}
        records: list[ModelShadowReviewRecord] = []
        dropped: list[ModelShadowSelection] = []
        for sel in selections:
            if sel.decision is not EnumShadowDecision.REVIEW or request.dry_run:
                continue
            candidate = by_key[sel.key]
            observed_at = self._clock()
            diff, reason = self._diff_source.diff(candidate)
            hits = scan_for_secrets(diff) if diff is not None else ()
            if diff is None or hits:
                drop = sel.model_copy(
                    update={
                        "decision": EnumShadowDecision.SKIP,
                        "reason": reason
                        if diff is None
                        else "secret-pattern:" + ",".join(hits),
                    }
                )
                dropped.append(drop)
                self._write(
                    root / "drops" / f"{store_name(sel.key)}.json",
                    json.dumps(
                        {
                            **drop.model_dump(mode="json"),
                            "head_sha": candidate.head_sha,
                            "observed_at": observed_at,
                        },
                        indent=2,
                    ),
                )
                continue
            work_dir = root / "artifacts" / store_name(sel.key)
            work_dir.mkdir(parents=True, exist_ok=True)
            self._write(work_dir / "diff.patch", diff)
            arms = self._reviewer.review(candidate, diff, sel.stratum, work_dir)
            record = ModelShadowReviewRecord(
                key=sel.key,
                repo=candidate.repo,
                number=candidate.number,
                stratum=sel.stratum,
                created_at=candidate.created_at,
                head_sha=candidate.head_sha,
                base=candidate.base,
                first_observed_at=observed_at,
                reviewed_at=self._clock(),
                diff_sha256=hashlib.sha256(diff.encode("utf-8")).hexdigest(),
                diff_bytes=len(diff.encode("utf-8")),
                arms=arms,
            )
            self._write(
                root / "records" / f"{store_name(sel.key)}.json",
                record.model_dump_json(indent=2),
            )
            records.append(record)
            if sel.stratum is EnumShadowStratum.PUBLIC:
                public_reviewed += 1
        result = ModelShadowReviewResult(
            store_root=root,
            window_start=request.policy.window_start,
            selections=selections,
            records=tuple(records),
            dropped=tuple(dropped),
            public_reviewed_total=public_reviewed,
            dry_run=request.dry_run,
        )
        if not request.dry_run:
            root.mkdir(parents=True, exist_ok=True)
            with (root / "ticks.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "at": self._clock(),
                            "window_start": request.policy.window_start,
                            "candidates": len(request.candidates),
                            "reviewed": [r.key for r in records],
                            "dropped": {d.key: d.reason for d in dropped},
                            "public_reviewed_total": public_reviewed,
                        }
                    )
                    + "\n"
                )
        return result
