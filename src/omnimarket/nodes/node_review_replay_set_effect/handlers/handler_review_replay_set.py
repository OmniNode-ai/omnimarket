# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Freeze the review replay set: merged diffs stratified by repo and size (OMN-20910).

The strata are repo x size bin. Candidates in a stratum are ordered by a hash of
the seed and the merge sha, and the strata are drawn round-robin, so the set is
balanced across strata, deterministic for a seed, and short only when a stratum
runs dry (``short_by``). Pinned items (diffs already measured) enter first. The
manifest hash is the SHA-256 of the manifest's canonical JSON, which carries the
SHA-256 of every diff, so a changed diff, a different selection or a different
seed is a different set. A directory named by the hash is written once and never
changed; a second freeze of the same set finds it intact or refuses.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Protocol

from omnimarket.nodes.node_review_replay_set_effect.handlers.adapter_git_merged_diffs import (
    GitMergedDiffSource,
)
from omnimarket.nodes.node_review_replay_set_effect.models import (
    SIZE_BINS,
    ModelMergedCandidate,
    ModelReplayItem,
    ModelReplayManifest,
    ModelReplayRepo,
    ModelReviewReplaySetRequest,
    ModelReviewReplaySetResult,
)

MANIFEST_FILE = "manifest.json"
DIFF_DIR = "diffs"


class ProtocolMergedDiffSource(Protocol):
    def list_merged(
        self, repo: ModelReplayRepo, limit: int
    ) -> list[ModelMergedCandidate]: ...

    def read_diff(
        self, repo: ModelReplayRepo, candidate: ModelMergedCandidate
    ) -> str: ...


def size_bin(changed_lines: int, upper: tuple[int, int]) -> str:
    """small up to ``upper[0]`` changed lines, medium up to ``upper[1]``, else large."""
    if changed_lines <= upper[0]:
        return SIZE_BINS[0]
    if changed_lines <= upper[1]:
        return SIZE_BINS[1]
    return SIZE_BINS[2]


def _order_key(seed: str, repo: str, candidate: ModelMergedCandidate) -> str:
    return hashlib.sha256(f"{seed}|{repo}|{candidate.merge_sha}".encode()).hexdigest()


def _ordered(
    seed: str, repo: str, members: list[ModelMergedCandidate]
) -> list[ModelMergedCandidate]:
    return sorted(members, key=lambda c: _order_key(seed, repo, c))


def manifest_json(manifest: ModelReplayManifest) -> str:
    return json.dumps(manifest.model_dump(mode="json"), sort_keys=True, indent=1) + "\n"


def manifest_hash(manifest: ModelReplayManifest) -> str:
    canonical = json.dumps(
        manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class HandlerReviewReplaySet:
    """Effect: read merged diffs, select the stratified set, freeze it under its hash."""

    def __init__(self, source: ProtocolMergedDiffSource | None = None) -> None:
        self._source: ProtocolMergedDiffSource = source or GitMergedDiffSource()

    def handle(
        self, request: ModelReviewReplaySetRequest
    ) -> ModelReviewReplaySetResult:
        repos = {r.repo: r for r in request.repos}
        by_repo: dict[str, list[ModelMergedCandidate]] = {
            name: self._source.list_merged(repo, request.history_limit)
            for name, repo in repos.items()
        }

        chosen: list[tuple[str, ModelMergedCandidate, bool]] = []
        taken: set[tuple[str, int]] = set()
        for pin in request.pinned:
            match = next(
                (c for c in by_repo.get(pin.repo, []) if c.pr_number == pin.pr_number),
                None,
            )
            if match is None:
                raise ValueError(
                    f"pinned item {pin.repo}#{pin.pr_number} is not a merged candidate "
                    f"in the last {request.history_limit} first-parent commits"
                )
            chosen.append((pin.repo, match, True))
            taken.add((pin.repo, pin.pr_number))

        strata: dict[tuple[str, str], list[ModelMergedCandidate]] = defaultdict(list)
        for name, candidates in by_repo.items():
            for candidate in candidates:
                if (name, candidate.pr_number) in taken:
                    continue
                bin_name = size_bin(
                    candidate.changed_lines, request.size_bin_upper_lines
                )
                strata[(name, bin_name)].append(candidate)
        for cell in strata:
            strata[cell] = _ordered(request.seed, cell[0], strata[cell])

        # Strata already holding a pinned item start one behind, so pins count toward balance.
        drawn: dict[tuple[str, str], int] = defaultdict(int)
        for name, candidate, _ in chosen:
            drawn[
                (name, size_bin(candidate.changed_lines, request.size_bin_upper_lines))
            ] += 1
        cells = sorted(
            set(strata)
            | {
                (n, size_bin(c.changed_lines, request.size_bin_upper_lines))
                for n, c, _ in chosen
            }
        )
        cursor = dict.fromkeys(cells, 0)
        while len(chosen) < request.target_count:
            live = [c for c in cells if cursor[c] < len(strata.get(c, []))]
            if not live:
                break
            live.sort(key=lambda c: (drawn[c], c))
            cell = live[0]
            chosen.append((cell[0], strata[cell][cursor[cell]], False))
            cursor[cell] += 1
            drawn[cell] += 1

        items: list[ModelReplayItem] = []
        diffs: dict[str, str] = {}
        for name, candidate, pinned in sorted(
            chosen, key=lambda t: (t[0], t[1].pr_number)
        ):
            diff = self._source.read_diff(repos[name], candidate)
            item_id = f"{name}-{candidate.pr_number}-{candidate.merge_sha[:12]}"
            diffs[item_id] = diff
            items.append(
                ModelReplayItem(
                    item_id=item_id,
                    repo=name,
                    pr_number=candidate.pr_number,
                    merge_sha=candidate.merge_sha,
                    parent_sha=candidate.parent_sha,
                    title=candidate.title,
                    changed_lines=candidate.changed_lines,
                    size_bin=size_bin(
                        candidate.changed_lines, request.size_bin_upper_lines
                    ),
                    diff_sha256=hashlib.sha256(diff.encode("utf-8")).hexdigest(),
                    diff_file=f"{DIFF_DIR}/{item_id}.diff",
                    pinned=pinned,
                )
            )

        manifest = ModelReplayManifest(
            seed=request.seed,
            target_count=request.target_count,
            size_bin_upper_lines=request.size_bin_upper_lines,
            items=tuple(items),
        )
        digest = manifest_hash(manifest)
        directory = Path(request.store_root) / digest
        manifest_path = directory / MANIFEST_FILE
        already = self._freeze(directory, manifest, diffs, digest)

        counts: dict[str, int] = defaultdict(int)
        for item in items:
            counts[f"{item.repo}/{item.size_bin}"] += 1
        return ModelReviewReplaySetResult(
            correlation_id=request.correlation_id,
            manifest_hash=digest,
            manifest_path=str(manifest_path),
            item_count=len(items),
            short_by=max(request.target_count - len(items), 0),
            strata=dict(sorted(counts.items())),
            already_frozen=already,
        )

    @staticmethod
    def _freeze(
        directory: Path,
        manifest: ModelReplayManifest,
        diffs: dict[str, str],
        digest: str,
    ) -> bool:
        manifest_path = directory / MANIFEST_FILE
        if manifest_path.exists():
            recorded = ModelReplayManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
            if manifest_hash(recorded) != digest or manifest_path.read_text(
                encoding="utf-8"
            ) != manifest_json(manifest):
                raise ValueError(
                    f"frozen manifest at {manifest_path} no longer matches its hash {digest}"
                )
            for item in recorded.items:
                body = (directory / item.diff_file).read_bytes()
                if hashlib.sha256(body).hexdigest() != item.diff_sha256:
                    raise ValueError(
                        f"frozen manifest diff {item.diff_file} no longer matches its hash"
                    )
            return True
        (directory / DIFF_DIR).mkdir(parents=True, exist_ok=True)
        for item in manifest.items:
            (directory / item.diff_file).write_text(
                diffs[item.item_id], encoding="utf-8"
            )
        manifest_path.write_text(manifest_json(manifest), encoding="utf-8")
        return False


__all__ = [
    "HandlerReviewReplaySet",
    "ProtocolMergedDiffSource",
    "manifest_hash",
    "manifest_json",
    "size_bin",
]
