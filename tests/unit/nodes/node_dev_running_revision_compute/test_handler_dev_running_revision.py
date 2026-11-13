# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Table tests of the running-revision reading, every rule in the order the handler checks them."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.models.running_revision import (
    EnumRunningRevisionRedReason,
    EnumRunningRevisionRefusalReason,
    EnumRunningRevisionStatus,
    EnumRunningRevisionUnknownReason,
    ModelBuildManifest,
    ModelDevMerge,
    ModelRepositoryMergeHistory,
    ModelRunningBuild,
    ModelRunningRevisionFacts,
    ModelRunningRevisionPolicy,
    ModelRunningRevisionReading,
    ModelRunningRevisionRefused,
)
from omnimarket.nodes.node_dev_running_revision_compute.handlers import (
    HandlerDevRunningRevision,
    read_running_revision,
)
from tests.unit.nodes.node_dev_running_revision_compute.conftest import (
    NOW,
    build,
    facts,
    history,
    merge,
    sha,
)

pytestmark = pytest.mark.unit

INFRA = history("omnibase_infra", sha(100), ())


def _reading(result: object) -> ModelRunningRevisionReading:
    assert isinstance(result, ModelRunningRevisionReading)
    return result


def _refused(result: object) -> ModelRunningRevisionRefused:
    assert isinstance(result, ModelRunningRevisionRefused)
    return result


def _repo(reading: ModelRunningRevisionReading, repository: str):
    return next(r for r in reading.repositories if r.repository == repository)


def test_running_at_the_tip_reads_current(policy: ModelRunningRevisionPolicy) -> None:
    b = build("b1", 60, {"omnimarket": 3, "omnibase_infra": 100})
    h = history("omnimarket", sha(0), (merge(1, 3000), merge(2, 2000), merge(3, 1000)))
    r = _reading(read_running_revision(facts((b,), (h, INFRA)), policy))
    assert r.status is EnumRunningRevisionStatus.CURRENT
    assert r.red is False
    assert r.red_reasons == ()
    repo = _repo(r, "omnimarket")
    assert repo.status is EnumRunningRevisionStatus.CURRENT
    assert repo.behind_by_changes == 0
    assert repo.running_sha == sha(3)


def test_behind_past_the_threshold_reads_red(
    policy: ModelRunningRevisionPolicy,
) -> None:
    b = build("b1", 60, {"omnimarket": 1, "omnibase_infra": 100})
    h = history(
        "omnimarket",
        sha(0),
        (merge(1, 6000), merge(2, 5400), merge(3, 4000), merge(4, 3000)),
    )
    r = _reading(read_running_revision(facts((b,), (h, INFRA)), policy))
    assert r.status is EnumRunningRevisionStatus.BEHIND
    assert r.red is True
    assert EnumRunningRevisionRedReason.BEHIND_PAST_THRESHOLD in r.red_reasons
    repo = _repo(r, "omnimarket")
    assert repo.status is EnumRunningRevisionStatus.BEHIND
    assert repo.behind_by_changes == 3
    assert repo.behind_for_seconds == 5400
    assert repo.oldest_not_running is not None
    assert repo.oldest_not_running.sha == sha(2)


def test_lagging_inside_the_threshold_reads_not_red(
    policy: ModelRunningRevisionPolicy,
) -> None:
    b = build("b1", 60, {"omnimarket": 1, "omnibase_infra": 100})
    h = history(
        "omnimarket",
        sha(0),
        (merge(1, 1500), merge(2, 1200), merge(3, 800), merge(4, 600)),
    )
    r = _reading(read_running_revision(facts((b,), (h, INFRA)), policy))
    repo = _repo(r, "omnimarket")
    assert repo.status is EnumRunningRevisionStatus.LAGGING
    assert repo.behind_by_changes == 3
    assert repo.behind_for_seconds == 1200
    assert r.red is False


def test_a_manifest_missing_a_repository_is_unknown_never_current(
    policy: ModelRunningRevisionPolicy,
) -> None:
    b = build("b1", 60, {"omnibase_infra": 100})
    h = history("omnimarket", sha(0), (merge(1, 3000),))
    r = _reading(read_running_revision(facts((b,), (h, INFRA)), policy))
    repo = _repo(r, "omnimarket")
    assert repo.status is EnumRunningRevisionStatus.UNKNOWN
    assert (
        repo.unknown_reason
        is EnumRunningRevisionUnknownReason.REVISION_MISSING_FROM_MANIFEST
    )
    assert r.status is not EnumRunningRevisionStatus.CURRENT
    assert r.red is True
    assert EnumRunningRevisionRedReason.UNKNOWN_REPOSITORY_READING in r.red_reasons


def test_no_builds_reads_no_running_build(policy: ModelRunningRevisionPolicy) -> None:
    h = history("omnimarket", sha(0), (merge(1, 3000),))
    r = _reading(read_running_revision(facts((), (h, INFRA)), policy))
    repo = _repo(r, "omnimarket")
    assert repo.status is EnumRunningRevisionStatus.UNKNOWN
    assert repo.unknown_reason is EnumRunningRevisionUnknownReason.NO_RUNNING_BUILD


def test_a_missing_history_reads_history_missing(
    policy: ModelRunningRevisionPolicy,
) -> None:
    b = build("b1", 60, {"omnibase_infra": 100})
    r = _reading(read_running_revision(facts((b,), ()), policy))
    repo = _repo(r, "omnimarket")
    assert repo.status is EnumRunningRevisionStatus.UNKNOWN
    assert repo.unknown_reason is EnumRunningRevisionUnknownReason.HISTORY_MISSING


def test_a_running_sha_in_neither_base_nor_merges_reads_unknown(
    policy: ModelRunningRevisionPolicy,
) -> None:
    b = build("b1", 60, {"omnimarket": 9, "omnibase_infra": 100})
    h = history("omnimarket", sha(0), (merge(1, 3000), merge(2, 2000)))
    r = _reading(read_running_revision(facts((b,), (h, INFRA)), policy))
    repo = _repo(r, "omnimarket")
    assert repo.status is EnumRunningRevisionStatus.UNKNOWN
    assert (
        repo.unknown_reason
        is EnumRunningRevisionUnknownReason.RUNNING_REVISION_NOT_IN_HISTORY
    )


def test_the_current_build_is_the_greatest_running_since(
    policy: ModelRunningRevisionPolicy,
) -> None:
    older = build("old", 3000, {"omnimarket": 1, "omnibase_infra": 100})
    newer = build("new", 60, {"omnimarket": 3, "omnibase_infra": 100})
    h = history("omnimarket", sha(0), (merge(1, 3000), merge(2, 2000), merge(3, 1000)))
    r = _reading(read_running_revision(facts((older, newer), (h, INFRA)), policy))
    assert r.current_build_id == "new"
    assert _repo(r, "omnimarket").status is EnumRunningRevisionStatus.CURRENT


def test_per_merge_time_contained_and_uncontained(
    policy: ModelRunningRevisionPolicy,
) -> None:
    b1 = build("b1", 3000, {"omnimarket": 2, "omnibase_infra": 100})
    b2 = build("b2", 60, {"omnimarket": 3, "omnibase_infra": 100})
    h = history(
        "omnimarket",
        sha(0),
        (merge(1, 5000), merge(2, 4000), merge(3, 3000), merge(4, 500)),
    )
    r = _reading(read_running_revision(facts((b1, b2), (h, INFRA)), policy))
    by_sha = {m.sha: m for m in r.merge_times if m.repository == "omnimarket"}
    assert by_sha[sha(1)].contained is True
    assert by_sha[sha(1)].seconds == 2000
    # the first build that contains the merge counts, not the later build that also does
    assert by_sha[sha(2)].running_since == NOW - timedelta(seconds=3000)
    assert by_sha[sha(2)].seconds == 1000
    assert by_sha[sha(3)].contained is True
    assert by_sha[sha(3)].seconds == 2940
    assert by_sha[sha(4)].contained is False
    assert by_sha[sha(4)].running_since is None
    assert by_sha[sha(4)].seconds == 500
    assert r.median_merge_to_running_seconds == 1000
    assert r.median_over_bound is False


def test_median_over_the_bound_reads_red(policy: ModelRunningRevisionPolicy) -> None:
    b = build("b1", 60, {"omnimarket": 1, "omnibase_infra": 100})
    h = history(
        "omnimarket",
        sha(0),
        (merge(1, 5000), merge(2, 4000), merge(3, 3000)),
    )
    r = _reading(read_running_revision(facts((b,), (h, INFRA)), policy))
    assert r.median_merge_to_running_seconds == 4000
    assert r.median_over_bound is True
    assert r.red is True
    assert EnumRunningRevisionRedReason.MEDIAN_OVER_BOUND in r.red_reasons


def test_two_histories_for_one_repository_are_refused(
    policy: ModelRunningRevisionPolicy,
) -> None:
    h1 = history("omnimarket", sha(0), (merge(1, 3000),))
    h2 = history("omnimarket", sha(0), (merge(2, 2000),))
    result = read_running_revision(facts((), (h1, h2)), policy)
    assert (
        _refused(result).reason is EnumRunningRevisionRefusalReason.DUPLICATE_REPOSITORY
    )


def test_a_repository_not_in_the_policy_is_refused(
    policy: ModelRunningRevisionPolicy,
) -> None:
    h = history("somewhere_else", sha(0), (merge(1, 3000),))
    result = read_running_revision(facts((), (h, INFRA)), policy)
    assert (
        _refused(result).reason is EnumRunningRevisionRefusalReason.UNKNOWN_REPOSITORY
    )


def test_a_merge_after_observed_at_is_refused(
    policy: ModelRunningRevisionPolicy,
) -> None:
    future = ModelDevMerge(sha=sha(1), merged_at=NOW + timedelta(seconds=10))
    h = history("omnimarket", sha(0), (future,))
    result = read_running_revision(facts((), (h, INFRA)), policy)
    assert (
        _refused(result).reason
        is EnumRunningRevisionRefusalReason.TIME_AFTER_OBSERVED_AT
    )


def test_a_history_whose_tip_is_not_the_newest_merge_raises() -> None:
    with pytest.raises(ValidationError):
        ModelRepositoryMergeHistory(
            repository="omnimarket",
            base_sha=sha(0),
            tip_sha=sha(1),
            merges=(merge(1, 3000), merge(2, 2000)),
        )


def test_an_unordered_history_raises() -> None:
    with pytest.raises(ValidationError):
        ModelRepositoryMergeHistory(
            repository="omnimarket",
            base_sha=sha(0),
            tip_sha=sha(2),
            merges=(merge(2, 3000), merge(1, 2000)),
        )


def test_a_running_since_before_built_at_raises() -> None:
    manifest = ModelBuildManifest(
        build_id="b",
        built_at=NOW,
        source_hash=None,
        revisions=(),
        branch_installs=(),
    )
    with pytest.raises(ValidationError):
        ModelRunningBuild(manifest=manifest, running_since=NOW - timedelta(seconds=60))


def test_a_naive_observed_at_raises() -> None:
    with pytest.raises(ValidationError):
        ModelRunningRevisionFacts(
            correlation_id=uuid4(),
            observed_at=datetime(2026, 11, 12, 16, 0),
            builds=(),
            histories=(),
        )


async def test_the_handler_with_no_policy_and_no_overlay_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(NodeOverlayNotFoundError):
        await HandlerDevRunningRevision().handle(facts((), ()))


def test_input_order_does_not_change_the_reading(
    policy: ModelRunningRevisionPolicy,
) -> None:
    b1 = build("b1", 3000, {"omnimarket": 2, "omnibase_infra": 100})
    b2 = build("b2", 60, {"omnimarket": 3, "omnibase_infra": 100})
    h1 = history("omnimarket", sha(0), (merge(1, 5000), merge(2, 4000), merge(3, 3000)))
    forward_facts = facts((b1, b2), (h1, INFRA))
    reversed_facts = forward_facts.model_copy(
        update={"builds": (b2, b1), "histories": (INFRA, h1)}
    )
    assert read_running_revision(forward_facts, policy) == read_running_revision(
        reversed_facts, policy
    )
