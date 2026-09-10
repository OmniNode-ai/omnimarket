# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18117: freshness is measured against the run's own materialisation.

The evidence-autoclose sweep materialises every product clone ONCE, near the
start of the job, and then spends ~20 minutes running ``dod_verify`` behaviour
checks against those trees. ``EvidenceCollector`` re-derived freshness at CHECK
time by fetching ``origin/<branch>`` again and comparing the clone's HEAD
against that fresh fetch's tip. So any ordinary merge landing on a product
repo's ``dev`` inside that window made an otherwise-correct clone read
``behind N``, and every behaviour check pinned there was refused UNEXECUTED as
``PRODUCT_CLONE_STALE`` — a "stale" verdict that is an artifact of the sweep's
own elapsed wall-clock time and says nothing about the ticket.

Measured: run 34428131180 (omnibase_infra, scheduled tick,
2026-09-10T02:06:28Z) held EIGHT tickets that way, each with zero failed checks
and no behaviour proven in either direction.

The fix pins the COMPARISON TARGET. The job records, per repository, the
upstream tip it materialised against, and the collector measures HEAD against
that recorded commit for the rest of the run instead of re-fetching. What it
must NOT do is weaken OMN-16846 AC5: a clone that was ALREADY behind when the
run materialised it still lacks the work under adjudication and is still
refused. Both directions are proven here against real git repositories.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
    EnumEvidenceUnverifiableCause,
    EnumProductCloneFreshness,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

pytestmark = pytest.mark.unit

_PIN_ENV = "DOD_VERIFY_PRODUCT_CLONE_PIN_FILE"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _seed_origin(tmp_path: Path) -> tuple[Path, Path]:
    """A real bare origin on ``dev`` plus the seed working tree that pushes it."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=dev", ".")

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--initial-branch=dev", ".")
    _git(seed, "config", "user.email", "t@t.invalid")
    _git(seed, "config", "user.name", "t")
    (seed / "marker.txt").write_text("v1\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "first")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", "dev")
    return origin, seed


def _advance_origin(seed: Path, filename: str, body: str) -> str:
    """Land one more commit on the origin's ``dev`` and return its SHA."""
    (seed / filename).write_text(body)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", f"add {filename}")
    _git(seed, "push", "origin", "dev")
    return _git(seed, "rev-parse", "HEAD")


def _clone(tmp_path: Path, origin: Path, name: str) -> Path:
    dest = tmp_path / name
    _git(tmp_path, "clone", str(origin), str(dest))
    return dest


def _write_pin_file(
    tmp_path: Path,
    *,
    repo_root: Path,
    pinned_sha: str,
    upstream_ref: str = "origin/dev",
    version: int = 1,
) -> Path:
    """Write the pin file the sweep's materialise step emits."""
    path = tmp_path / "product-clone-pins.json"
    path.write_text(
        json.dumps(
            {
                "version": version,
                "pins": [
                    {
                        "repo_root": str(repo_root),
                        "pinned_sha": pinned_sha,
                        "upstream_ref": upstream_ref,
                        "recorded_at": "2026-09-10T02:06:28Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _item(cwd: Path, command: str) -> dict[str, object]:
    return {
        "id": "dod-occ-diff-derived-behavior-proof",
        "description": "behaviour check bound to a product clone",
        "checks": [
            {
                "check_type": "test_passes",
                "command": command,
                "cwd": str(cwd),
            }
        ],
    }


@pytest.fixture
def collector(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> EvidenceCollector:
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.delenv("DOD_VERIFY_ALLOW_STALE_PRODUCT_CLONE", raising=False)
    monkeypatch.delenv(_PIN_ENV, raising=False)
    monkeypatch.setenv("DOD_VERIFY_LIVE_PR_CHECK", "0")
    return EvidenceCollector()


# ---------------------------------------------------------------------------
# The defect, stated without the fix
# ---------------------------------------------------------------------------


def test_a_mid_run_merge_alone_refuses_a_clone_that_contains_the_work(
    collector: EvidenceCollector, tmp_path: Path
) -> None:
    """RED-first premise: no pin, so a later unrelated merge is enough.

    ``proof.txt`` — the work under adjudication — IS in this clone. The only
    thing that changed is that somebody else merged to ``dev`` afterwards.
    """
    origin, seed = _seed_origin(tmp_path)
    _advance_origin(seed, "proof.txt", "the work under adjudication\n")
    runner = _clone(tmp_path, origin, "runner")
    assert (runner / "proof.txt").exists()

    _advance_origin(seed, "unrelated.txt", "somebody else's merge\n")

    result = collector._check_evidence_item(
        _item(runner, "test -f proof.txt"), "OMN-18117"
    )

    assert result.status is EnumEvidenceCheckStatus.SKIPPED
    assert (
        result.unverifiable_cause is EnumEvidenceUnverifiableCause.PRODUCT_CLONE_STALE
    )


# ---------------------------------------------------------------------------
# AC1 — the pinned verdict is invariant over a mid-run merge
# ---------------------------------------------------------------------------


def test_a_mid_run_merge_does_not_change_a_pinned_verdict(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same tree, same command, same later merge — pinned, so the check runs."""
    origin, seed = _seed_origin(tmp_path)
    sha_a = _advance_origin(seed, "proof.txt", "the work under adjudication\n")
    runner = _clone(tmp_path, origin, "runner")
    pin_file = _write_pin_file(tmp_path, repo_root=runner, pinned_sha=sha_a)
    monkeypatch.setenv(_PIN_ENV, str(pin_file))

    _advance_origin(seed, "unrelated.txt", "somebody else's merge\n")

    result = collector._check_evidence_item(
        _item(runner, "test -f proof.txt"), "OMN-18117"
    )

    assert result.status is EnumEvidenceCheckStatus.VERIFIED
    assert result.unverifiable_cause is None
    clone = result.product_clones[0]
    assert clone.freshness is EnumProductCloneFreshness.FRESH
    assert clone.behind_count == 0


def test_the_pinned_resolution_records_what_it_compared_against(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A reader of the receipt can tell a pinned verdict from a live-fetch one."""
    origin, seed = _seed_origin(tmp_path)
    sha_a = _advance_origin(seed, "proof.txt", "work\n")
    runner = _clone(tmp_path, origin, "runner")
    pin_file = _write_pin_file(tmp_path, repo_root=runner, pinned_sha=sha_a)
    monkeypatch.setenv(_PIN_ENV, str(pin_file))
    _advance_origin(seed, "unrelated.txt", "later\n")

    result = collector._check_evidence_item(_item(runner, "true"), "OMN-18117")

    clone = result.product_clones[0]
    assert clone.comparison_pinned is True
    assert clone.comparison_sha == sha_a
    assert clone.head_sha == sha_a
    assert clone.upstream_ref == "origin/dev"


def test_a_pinned_run_never_fetches_the_moving_tip(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pin is the comparison target, so there is nothing to re-fetch.

    Asserted on the git argv rather than on the verdict: a run that still
    fetched would be one upstream advance away from the defect returning.
    """
    origin, seed = _seed_origin(tmp_path)
    sha_a = _advance_origin(seed, "proof.txt", "work\n")
    runner = _clone(tmp_path, origin, "runner")
    pin_file = _write_pin_file(tmp_path, repo_root=runner, pinned_sha=sha_a)
    monkeypatch.setenv(_PIN_ENV, str(pin_file))

    seen: list[tuple[str, ...]] = []
    original = collector._run_git

    def _recording(repo: Path, *args: str) -> tuple[int, str, str]:
        seen.append(args)
        return original(repo, *args)

    monkeypatch.setattr(collector, "_run_git", _recording)
    collector._check_evidence_item(_item(runner, "true"), "OMN-18117")

    assert not [args for args in seen if args and args[0] == "fetch"]


# ---------------------------------------------------------------------------
# AC2 — OMN-16846 AC5 is not weakened
# ---------------------------------------------------------------------------


def test_a_clone_behind_its_own_pin_is_still_refused(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The clone never contained the merge under adjudication. Still refused."""
    origin, seed = _seed_origin(tmp_path)
    behind = _clone(tmp_path, origin, "behind")
    sha_a = _advance_origin(seed, "proof.txt", "the work under adjudication\n")
    # The run materialised against sha_a; this tree predates it.
    _git(behind, "fetch", "origin", "dev")
    pin_file = _write_pin_file(tmp_path, repo_root=behind, pinned_sha=sha_a)
    monkeypatch.setenv(_PIN_ENV, str(pin_file))

    result = collector._check_evidence_item(
        _item(behind, "test -f proof.txt"), "OMN-18117"
    )

    assert result.status is EnumEvidenceCheckStatus.SKIPPED
    assert (
        result.unverifiable_cause is EnumEvidenceUnverifiableCause.PRODUCT_CLONE_STALE
    )
    clone = result.product_clones[0]
    assert clone.freshness is EnumProductCloneFreshness.STALE
    assert clone.behind_count == 1
    assert clone.comparison_pinned is True
    assert clone.comparison_sha == sha_a


def test_the_refused_pinned_command_is_never_executed(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fail closed still means NOT RUN, proven by a side effect."""
    origin, seed = _seed_origin(tmp_path)
    behind = _clone(tmp_path, origin, "behind")
    sha_a = _advance_origin(seed, "proof.txt", "work\n")
    _git(behind, "fetch", "origin", "dev")
    pin_file = _write_pin_file(tmp_path, repo_root=behind, pinned_sha=sha_a)
    monkeypatch.setenv(_PIN_ENV, str(pin_file))
    canary = behind / "executed.canary"

    result = collector._check_evidence_item(
        _item(behind, f"touch {canary}"), "OMN-18117"
    )

    assert result.status is EnumEvidenceCheckStatus.SKIPPED
    assert not canary.exists()


def test_a_pin_absent_from_the_object_database_fails_closed(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pinned commit this clone has never heard of cannot be contained by HEAD.

    This is the shallow-clone shape: the tree was copied at an older SHA and
    the pinned tip's object was never fetched into it, so the commit count is
    not computable. STALE, not FRESH, and not UNKNOWN.
    """
    origin, seed = _seed_origin(tmp_path)
    behind = _clone(tmp_path, origin, "behind")
    sha_a = _advance_origin(seed, "proof.txt", "work\n")
    # Deliberately NO fetch into `behind`: sha_a is unknown to it.
    pin_file = _write_pin_file(tmp_path, repo_root=behind, pinned_sha=sha_a)
    monkeypatch.setenv(_PIN_ENV, str(pin_file))

    result = collector._check_evidence_item(
        _item(behind, "test -f proof.txt"), "OMN-18117"
    )

    assert result.status is EnumEvidenceCheckStatus.SKIPPED
    assert (
        result.unverifiable_cause is EnumEvidenceUnverifiableCause.PRODUCT_CLONE_STALE
    )
    assert result.product_clones[0].freshness is EnumProductCloneFreshness.STALE


def test_a_dirty_pinned_clone_is_still_dirty(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pinning changes the comparison target, not the tracked-dirt refusal."""
    origin, seed = _seed_origin(tmp_path)
    sha_a = _advance_origin(seed, "proof.txt", "work\n")
    runner = _clone(tmp_path, origin, "runner")
    pin_file = _write_pin_file(tmp_path, repo_root=runner, pinned_sha=sha_a)
    monkeypatch.setenv(_PIN_ENV, str(pin_file))
    (runner / "marker.txt").write_text("locally edited\n")

    result = collector._check_evidence_item(_item(runner, "true"), "OMN-18117")

    assert result.status is EnumEvidenceCheckStatus.SKIPPED
    assert (
        result.unverifiable_cause
        is EnumEvidenceUnverifiableCause.PRODUCT_CLONE_FRESHNESS_UNKNOWN
    )
    assert result.product_clones[0].freshness is EnumProductCloneFreshness.DIRTY


# ---------------------------------------------------------------------------
# Scope — a pin binds one repository, and its absence changes nothing
# ---------------------------------------------------------------------------


def test_a_repository_with_no_pin_keeps_the_live_fetch_behaviour(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The local operator path has no pin file entry and must be untouched."""
    origin, seed = _seed_origin(tmp_path)
    sha_a = _advance_origin(seed, "proof.txt", "work\n")
    runner = _clone(tmp_path, origin, "runner")
    other = _clone(tmp_path, origin, "other")
    # The pin file names `other`, not `runner`.
    pin_file = _write_pin_file(tmp_path, repo_root=other, pinned_sha=sha_a)
    monkeypatch.setenv(_PIN_ENV, str(pin_file))

    _advance_origin(seed, "unrelated.txt", "later\n")

    result = collector._check_evidence_item(
        _item(runner, "test -f proof.txt"), "OMN-18117"
    )

    assert result.status is EnumEvidenceCheckStatus.SKIPPED
    assert (
        result.unverifiable_cause is EnumEvidenceUnverifiableCause.PRODUCT_CLONE_STALE
    )
    assert result.product_clones[0].comparison_pinned is False


def test_an_unreadable_pin_file_falls_back_to_the_live_comparison(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A malformed pin file must never read as "everything is pinned and fresh".

    Falling back lands on the pre-existing fail-closed refusal, which is the
    conservative direction.
    """
    origin, seed = _seed_origin(tmp_path)
    _advance_origin(seed, "proof.txt", "work\n")
    runner = _clone(tmp_path, origin, "runner")
    bad = tmp_path / "broken-pins.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv(_PIN_ENV, str(bad))

    _advance_origin(seed, "unrelated.txt", "later\n")

    result = collector._check_evidence_item(
        _item(runner, "test -f proof.txt"), "OMN-18117"
    )

    assert result.status is EnumEvidenceCheckStatus.SKIPPED
    assert result.product_clones[0].comparison_pinned is False


def test_a_pin_file_of_an_unrecognised_schema_version_is_ignored(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    origin, seed = _seed_origin(tmp_path)
    sha_a = _advance_origin(seed, "proof.txt", "work\n")
    runner = _clone(tmp_path, origin, "runner")
    pin_file = _write_pin_file(tmp_path, repo_root=runner, pinned_sha=sha_a, version=99)
    monkeypatch.setenv(_PIN_ENV, str(pin_file))

    _advance_origin(seed, "unrelated.txt", "later\n")

    result = collector._check_evidence_item(
        _item(runner, "test -f proof.txt"), "OMN-18117"
    )

    assert result.status is EnumEvidenceCheckStatus.SKIPPED
    assert result.product_clones[0].comparison_pinned is False


def test_the_pin_file_is_read_once_per_collector(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A sweep adjudicates dozens of candidates; the file is not re-read each time."""
    origin, seed = _seed_origin(tmp_path)
    sha_a = _advance_origin(seed, "proof.txt", "work\n")
    runner_a = _clone(tmp_path, origin, "runner_a")
    runner_b = _clone(tmp_path, origin, "runner_b")
    path = tmp_path / "product-clone-pins.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "pins": [
                    {"repo_root": str(runner_a), "pinned_sha": sha_a},
                    {"repo_root": str(runner_b), "pinned_sha": sha_a},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(_PIN_ENV, str(path))

    reads = 0
    original_read = Path.read_text

    def _counting(self: Path, *args: object, **kwargs: object) -> str:
        nonlocal reads
        if self == path:
            reads += 1
        return original_read(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _counting)

    collector._check_evidence_item(_item(runner_a, "true"), "OMN-18117")
    collector._check_evidence_item(_item(runner_b, "true"), "OMN-18117")

    assert reads == 1
