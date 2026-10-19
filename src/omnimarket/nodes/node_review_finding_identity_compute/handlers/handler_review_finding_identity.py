# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_review_finding_identity_compute (OMN-21190).

Pure COMPUTE. Produces the occurrence key of a finding and carries its lineage
across snapshots. The key is a namespaced UUIDv5 over the repository, the PR number
and immutable snapshot id, the path, a hash of the normalized hunk, the category and
a claim-instance discriminator (rule id, hash of the normalized claim text, and the
producer's ordinal of the claim within the hunk). A later snapshot is a different
occurrence; the lineage id, fixed by the first occurrence, is what ties them.

Re-anchoring finds the prior hunk's normalized lines in the target file: an exact
block first, then the most similar block at or above min_similarity. A hunk it
cannot find is ANCHOR_LOST: no key is produced for it, the lineage is kept, and it
is counted.
"""

from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher
from uuid import UUID, uuid5

from omnimarket.nodes.node_review_finding_identity_compute.models.enum_anchor_status import (
    EnumAnchorLostReason,
    EnumAnchorStatus,
)
from omnimarket.nodes.node_review_finding_identity_compute.models.model_review_finding_identity import (
    ModelAnchorCounts,
    ModelFindingIdentityInput,
    ModelFindingIdentityOutput,
    ModelIdentityFinding,
    ModelReanchorRequest,
    ModelResolvedIdentity,
)

OCCURRENCE_NAMESPACE = UUID("8d2f6c1e-4b7a-5a93-9e0d-211900c0ffe1")

_WHITESPACE = re.compile(r"\s+")
_HUNK_HEADER = re.compile(r"^@@.*@@")


def _normalized_lines(text: str) -> list[tuple[int, str]]:
    """(1-based line number, normalized line) for each line that is not blank or an @@ header."""
    out: list[tuple[int, str]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = _WHITESPACE.sub(" ", raw).strip()
        if line and not _HUNK_HEADER.match(line):
            out.append((number, line))
    return out


def hunk_hash_for(hunk_text: str) -> str:
    """Hash of the normalized hunk: stable across whitespace, blank lines, headers and line numbers."""
    body = "\n".join(line for _, line in _normalized_lines(hunk_text))
    return hashlib.sha256(body.encode()).hexdigest()


def claim_hash_for(claim_text: str) -> str:
    """Hash of the claim text, case and whitespace folded."""
    return hashlib.sha256(
        _WHITESPACE.sub(" ", claim_text).strip().lower().encode()
    ).hexdigest()


def occurrence_key_for(finding: ModelIdentityFinding, hunk_hash: str) -> str:
    """Deterministic, namespaced occurrence key for a finding on its anchor."""
    canonical = json.dumps(
        [
            finding.repository,
            finding.pr_number,
            finding.snapshot_id,
            finding.path,
            hunk_hash,
            finding.category,
            finding.rule_id or "",
            claim_hash_for(finding.claim_text),
            finding.claim_ordinal,
        ],
        separators=(",", ":"),
    )
    return str(uuid5(OCCURRENCE_NAMESPACE, canonical))


def _find_block(
    needle: list[str],
    haystack: list[tuple[int, str]],
    prior_line: int | None,
    min_similarity: float,
) -> tuple[int, list[str]] | None:
    """The best-matching block of the haystack as (start line, lines), or None.

    Similarity is character-level over the joined lines. Exact blocks win; among equals the one nearest the prior line wins, then the first.
    """
    size = len(needle)
    lines = [line for _, line in haystack]
    target = "\n".join(needle)
    best: tuple[float, int, int] | None = None  # (-ratio, distance, index)
    for index in range(len(lines) - size + 1):
        window = lines[index : index + size]
        if window == needle:
            ratio = 1.0
        else:
            matcher = SequenceMatcher(None, target, "\n".join(window))
            if (
                matcher.real_quick_ratio() < min_similarity
                or matcher.quick_ratio() < min_similarity
            ):
                continue
            ratio = matcher.ratio()
        if ratio < min_similarity:
            continue
        distance = abs(haystack[index][0] - prior_line) if prior_line is not None else 0
        candidate = (-ratio, distance, index)
        if best is None or candidate < best:
            best = candidate
    if best is None:
        return None
    index = best[2]
    return haystack[index][0], lines[index : index + size]


class HandlerReviewFindingIdentityCompute:
    """COMPUTE: occurrence keys, lineage across snapshots, ANCHOR_LOST."""

    def handle(self, request: ModelFindingIdentityInput) -> ModelFindingIdentityOutput:
        identities = [self._new(finding) for finding in request.new_findings]
        identities.extend(
            self._reanchor(item, request.min_similarity) for item in request.reanchors
        )
        counts = ModelAnchorCounts(
            new=sum(1 for i in identities if i.status is EnumAnchorStatus.NEW),
            unchanged=sum(
                1 for i in identities if i.status is EnumAnchorStatus.UNCHANGED
            ),
            moved=sum(1 for i in identities if i.status is EnumAnchorStatus.MOVED),
            anchor_lost=sum(
                1 for i in identities if i.status is EnumAnchorStatus.ANCHOR_LOST
            ),
            total=len(identities),
        )
        return ModelFindingIdentityOutput(
            correlation_id=request.correlation_id,
            identities=tuple(identities),
            counts=counts,
        )

    @staticmethod
    def _new(finding: ModelIdentityFinding) -> ModelResolvedIdentity:
        hunk_hash = hunk_hash_for(finding.hunk_text)
        key = occurrence_key_for(finding, hunk_hash)
        return ModelResolvedIdentity(
            finding_ref=finding.finding_ref,
            status=EnumAnchorStatus.NEW,
            lineage_id=key,
            occurrence_key=key,
            hunk_hash=hunk_hash,
            start_line=finding.start_line,
        )

    @staticmethod
    def _lost(
        item: ModelReanchorRequest, reason: EnumAnchorLostReason
    ) -> ModelResolvedIdentity:
        return ModelResolvedIdentity(
            finding_ref=item.finding_ref,
            status=EnumAnchorStatus.ANCHOR_LOST,
            lineage_id=item.prior.lineage_id,
            occurrence_key=None,
            prior_occurrence_key=item.prior.occurrence_key,
            lost_reason=reason,
        )

    def _reanchor(
        self, item: ModelReanchorRequest, min_similarity: float
    ) -> ModelResolvedIdentity:
        prior = item.prior.finding
        needle = [line for _, line in _normalized_lines(prior.hunk_text)]
        if not needle:
            return self._lost(item, EnumAnchorLostReason.NO_ANCHOR_LINES)
        if item.target_file_text is None:
            return self._lost(item, EnumAnchorLostReason.FILE_UNAVAILABLE)
        found = _find_block(
            needle,
            _normalized_lines(item.target_file_text),
            prior.start_line,
            min_similarity,
        )
        if found is None:
            return self._lost(item, EnumAnchorLostReason.HUNK_NOT_FOUND)
        start_line, block = found
        hunk_hash = hashlib.sha256("\n".join(block).encode()).hexdigest()
        moved_finding = prior.model_copy(
            update={
                "snapshot_id": item.target_snapshot_id,
                "path": item.target_path or prior.path,
            }
        )
        moved = prior.start_line is not None and prior.start_line != start_line
        return ModelResolvedIdentity(
            finding_ref=item.finding_ref,
            status=EnumAnchorStatus.MOVED if moved else EnumAnchorStatus.UNCHANGED,
            lineage_id=item.prior.lineage_id,
            occurrence_key=occurrence_key_for(moved_finding, hunk_hash),
            prior_occurrence_key=item.prior.occurrence_key,
            hunk_hash=hunk_hash,
            start_line=start_line,
        )
