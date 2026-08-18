# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OCC companion emitter: stale-base force-push guard (OMN-15845 / OMN-16116).

``_clone_and_branch`` performs a single shallow clone and captures ``base_sha``
once; the two force-pushes that follow it never merge/rebase the working
branch. If a DIFFERENT product PR's companion for the SAME ticket merges to
OCC's default branch between this run's clone and either of its pushes, this
run has no way to detect it: ``contract_already_had_companion`` was evaluated
against the now-stale clone snapshot, so it silently re-writes the
ticket-scoped ``dod-occ-evidence-admissibility-validator`` receipt (meant to
be write-once per ticket) and force-pushes a stale full-regenerate diff that
either orphans the sibling companion or produces an add/add conflict at merge
time.

Live incident (OMN-15845's own observed timeline): three sibling autobind
companions minted for the same ticket ~70 minutes apart; the one that never
picked up a sibling's merge was orphaned unmergeable.

OMN-16116 (round-2 adversarial-review narrowing): a raw SHA mismatch is NOT
itself a collision — OCC's default branch churns roughly every 24 minutes on
average, almost always on OTHER tickets. The guard now fetches the single new
commit and diffs it against ``base_sha``, raising ``StaleCompanionBaseError``
ONLY when the diff touches THIS run's own ticket-scoped paths
(``contracts/<ticket>.yaml`` or ``drift/dod_receipts/<ticket>/**``); an
unrelated-ticket move on OCC's default branch must NOT abort the run.

This suite drives the REAL ``OccCompanionEmitter._emit_companion_sync`` (the
live producer, per ``reference_two_occ_producers_canonical_not_wired`` — NOT
the unwired ``node_occ_companion_compute`` oracle) with git/network I/O
mocked, mirroring the harness in
``test_occ_companion_emitter_friction_omn_14741.py``. It asserts:

  * a moved OCC default branch whose diff touches THIS run's own ticket
    (a same-ticket collision), detected via the cheap ``git ls-remote``
    freshness check + scoped diff immediately before a force-push, raises
    ``StaleCompanionBaseError`` and the push is never attempted;
  * a moved OCC default branch whose diff touches only an UNRELATED
    ticket's paths does NOT raise and the run proceeds to push normally
    (the OMN-16116 regression this round exists to prevent — RED-proven
    against the naive SHA-only predicate);
  * the OMN-14793 lease is still released in the ``finally`` on the
    fail-fast path;
  * the unchanged-base happy path is unaffected — both force-pushes still
    fire normally (the load-bearing regression guard for this fix).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    OccCompanionEmitter,
    StaleCompanionBaseError,
)

_MOD = "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter"

# The fixed base SHA _clone_and_branch is mocked to return in this suite's harness.
_BASE_SHA = "0" * 40
_MOVED_SHA = "9" * 40

# The ticket ``_extract_tickets`` derives from ``_default_pr_data``'s title
# ("feat(OMN-9999): the thing") via TICKET_PATTERN fallback. Used to build a
# same-ticket-collision diff (this run's own path) vs an unrelated-ticket diff
# (a different ticket's path — must NOT trip the guard, OMN-16116).
_OWN_TICKET = "OMN-9999"
_OTHER_TICKET = "OMN-1234"
_SAME_TICKET_DIFF = [f"contracts/{_OWN_TICKET}.yaml"]
_OTHER_TICKET_DIFF = [f"contracts/{_OTHER_TICKET}.yaml"]


@pytest.fixture(autouse=True)
def _pin_legacy_check_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``OMNI_OCC_CHECK_BINDING=pr_existence`` (see the sibling friction
    suite's identical fixture docstring): this suite's property (the stale-base
    guard) is binding-orthogonal, and driving the default content-bound path
    without injecting a RED-derivable diff would take the unrelated
    ``skip:NO_RED_DERIVABLE_CHECK`` branch instead of reaching the pushes this
    suite tests.
    """
    monkeypatch.setenv("OMNI_OCC_CHECK_BINDING", "pr_existence")


class _FakeTempDir:
    """A ``tempfile.TemporaryDirectory`` stand-in yielding a fixed path."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def __enter__(self) -> str:
        return str(self._path)

    def __exit__(self, *_exc: object) -> bool:
        return False


def _default_pr_data() -> dict[str, object]:
    return {
        "body": "Implements the thing.",
        "title": "feat(OMN-9999): the thing",
        "head": {"sha": "b" * 40, "ref": "feature-branch"},
        "state": "open",
        "draft": False,
        "labels": [],
    }


def _run_emit(
    emitter: OccCompanionEmitter,
    tmp_path: Path,
    *,
    ls_remote_shas: list[str],
    lease_release: MagicMock | None = None,
    diff_paths: list[str] | None = None,
) -> tuple[str, list[list[str]]]:
    """Drive the REAL ``_emit_companion_sync`` with a controllable ``ls-remote``.

    ``ls_remote_shas`` supplies the remote HEAD SHA returned on each successive
    ``git ls-remote`` call, in order (one entry consumed per freshness check —
    there are up to two, one before each force-push). Once exhausted, further
    calls fall back to ``_BASE_SHA`` (fresh) so a test only needs to specify the
    calls it cares about.

    ``diff_paths`` supplies the changed-file list ``git diff --name-only``
    returns when a freshness check's ``ls-remote`` SHA differs from
    ``_BASE_SHA`` (OMN-16116 scoped-diff narrowing) — defaults to a
    same-ticket collision (``contracts/OMN-9999.yaml``) so callers that don't
    care about the narrowing still exercise the pre-existing collision path.
    """
    git_calls: list[list[str]] = []
    remaining_ls_remote = list(ls_remote_shas)
    release_target = lease_release if lease_release is not None else MagicMock()
    diff_lines = "\n".join(diff_paths if diff_paths is not None else _SAME_TICKET_DIFF)

    def fake_rest(method: str, path: str, *, body=None, token=None) -> dict:
        if path.endswith("/pulls/321"):  # product PR GET
            return dict(_default_pr_data())
        if "/pulls/55" in path:  # OCC PR GET after open-or-sync
            return {"number": 55, "state": "open"}
        return {}

    def fake_run_git(argv: list[str], *, cwd: str) -> str:
        git_calls.append(argv)
        if "rev-parse" in argv:
            return "c" * 40
        if "ls-remote" in argv:
            sha = remaining_ls_remote.pop(0) if remaining_ls_remote else _BASE_SHA
            return f"{sha}\tHEAD\n"
        if "diff" in argv and "--name-only" in argv:
            return diff_lines
        if "fetch" in argv:
            return ""
        return ""

    def fake_clone(cd: Path, *_a: object) -> str:
        cd.mkdir(parents=True, exist_ok=True)
        return _BASE_SHA

    with (
        patch(f"{_MOD}.rest_json", side_effect=fake_rest),
        patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
        patch(f"{_MOD}.acquire_occ_companion_lease", return_value=True),
        patch(f"{_MOD}.release_occ_companion_lease", release_target),
        patch.object(emitter, "_run_git", side_effect=fake_run_git),
        patch.object(emitter, "_clone_and_branch", side_effect=fake_clone),
        patch.object(emitter, "_open_or_sync_occ_pr", return_value=55),
        patch.object(emitter, "_observe_pr_probe", return_value=("{}", 0)),
        patch.object(emitter, "_patch_evidence_source"),
        patch(
            f"{_MOD}.tempfile.TemporaryDirectory",
            return_value=_FakeTempDir(tmp_path),
        ),
    ):
        action = emitter._emit_companion_sync("OmniNode-ai/omnimarket", 321, None)
    return action, git_calls


def _push_calls(git_calls: list[list[str]]) -> list[list[str]]:
    return [c for c in git_calls if "push" in c and "--force" in c]


def _ls_remote_calls(git_calls: list[list[str]]) -> list[list[str]]:
    return [c for c in git_calls if "ls-remote" in c]


@pytest.mark.unit
class TestStaleBaseGuard:
    def test_stale_base_raises_with_both_shas_named(self, tmp_path: Path) -> None:
        """RED against the pre-fix emitter: no freshness check existed, so the
        force-push ran unconditionally. GREEN: a moved OCC default branch is
        detected before the FIRST force-push, and the error names both the
        captured base and the observed remote SHA."""
        emitter = OccCompanionEmitter()
        release_mock = MagicMock()

        with pytest.raises(StaleCompanionBaseError, match=_BASE_SHA) as excinfo:
            _run_emit(
                emitter,
                tmp_path,
                ls_remote_shas=[_MOVED_SHA],
                lease_release=release_mock,
            )
        assert _MOVED_SHA in str(excinfo.value)
        release_mock.assert_called_once()

    def test_stale_base_never_reaches_force_push(self, tmp_path: Path) -> None:
        """Direct assertion on the captured git call sequence: the freshness
        check (`git ls-remote`) fires, but `git push --force` never does."""
        emitter = OccCompanionEmitter()
        git_calls: list[list[str]] = []
        remaining = [_MOVED_SHA]

        def fake_rest(method: str, path: str, *, body=None, token=None) -> dict:
            if path.endswith("/pulls/321"):
                return dict(_default_pr_data())
            return {}

        def fake_run_git(argv: list[str], *, cwd: str) -> str:
            git_calls.append(argv)
            if "rev-parse" in argv:
                return "c" * 40
            if "ls-remote" in argv:
                sha = remaining.pop(0) if remaining else _BASE_SHA
                return f"{sha}\tHEAD\n"
            if "diff" in argv and "--name-only" in argv:
                # Same-ticket collision: OMN-9999 is the ticket this run
                # itself owns (see ``_default_pr_data``'s title).
                return "\n".join(_SAME_TICKET_DIFF)
            if "fetch" in argv:
                return ""
            return ""

        def fake_clone(cd: Path, *_a: object) -> str:
            cd.mkdir(parents=True, exist_ok=True)
            return _BASE_SHA

        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
            patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
            patch(f"{_MOD}.acquire_occ_companion_lease", return_value=True),
            patch(f"{_MOD}.release_occ_companion_lease", MagicMock()),
            patch.object(emitter, "_run_git", side_effect=fake_run_git),
            patch.object(emitter, "_clone_and_branch", side_effect=fake_clone),
            patch.object(emitter, "_open_or_sync_occ_pr", return_value=55),
            patch.object(emitter, "_observe_pr_probe", return_value=("{}", 0)),
            patch.object(emitter, "_patch_evidence_source"),
            patch(
                f"{_MOD}.tempfile.TemporaryDirectory",
                return_value=_FakeTempDir(tmp_path),
            ),
            pytest.raises(StaleCompanionBaseError),
        ):
            emitter._emit_companion_sync("OmniNode-ai/omnimarket", 321, None)

        assert _ls_remote_calls(git_calls), "freshness check must have run"
        assert _push_calls(git_calls) == [], (
            "a stale base must be caught before ANY force-push — got push "
            f"calls: {_push_calls(git_calls)!r}"
        )

    def test_moved_between_first_and_second_push_raises_on_second(
        self, tmp_path: Path
    ) -> None:
        """The freshness check re-fires before EACH push: a sibling write that
        lands between the first and second push (two GitHub round-trips —
        open-or-sync PR + self-bind probe — separate them) is caught too."""
        emitter = OccCompanionEmitter()
        git_calls: list[list[str]] = []
        # First check: fresh (matches base). Second check: moved.
        remaining = [_BASE_SHA, _MOVED_SHA]

        def fake_rest(method: str, path: str, *, body=None, token=None) -> dict:
            if path.endswith("/pulls/321"):
                return dict(_default_pr_data())
            if "/pulls/55" in path:
                return {"number": 55, "state": "open"}
            return {}

        def fake_run_git(argv: list[str], *, cwd: str) -> str:
            git_calls.append(argv)
            if "rev-parse" in argv:
                return "c" * 40
            if "ls-remote" in argv:
                sha = remaining.pop(0) if remaining else _BASE_SHA
                return f"{sha}\tHEAD\n"
            if "diff" in argv and "--name-only" in argv:
                # Same-ticket collision: OMN-9999 is the ticket this run
                # itself owns (see ``_default_pr_data``'s title).
                return "\n".join(_SAME_TICKET_DIFF)
            if "fetch" in argv:
                return ""
            return ""

        def fake_clone(cd: Path, *_a: object) -> str:
            cd.mkdir(parents=True, exist_ok=True)
            return _BASE_SHA

        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
            patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
            patch(f"{_MOD}.acquire_occ_companion_lease", return_value=True),
            patch(f"{_MOD}.release_occ_companion_lease", MagicMock()),
            patch.object(emitter, "_run_git", side_effect=fake_run_git),
            patch.object(emitter, "_clone_and_branch", side_effect=fake_clone),
            patch.object(emitter, "_open_or_sync_occ_pr", return_value=55),
            patch.object(emitter, "_observe_pr_probe", return_value=("{}", 0)),
            patch.object(emitter, "_patch_evidence_source"),
            patch(
                f"{_MOD}.tempfile.TemporaryDirectory",
                return_value=_FakeTempDir(tmp_path),
            ),
            pytest.raises(StaleCompanionBaseError),
        ):
            emitter._emit_companion_sync("OmniNode-ai/omnimarket", 321, None)

        assert len(_push_calls(git_calls)) == 1, (
            "exactly ONE force-push (the first) must have happened before the "
            f"second freshness check caught the move: {_push_calls(git_calls)!r}"
        )

    def test_unchanged_base_still_pushes_normally(self, tmp_path: Path) -> None:
        """Regression guard: the fresh (unchanged-base) happy path is NOT
        broken by the new check — both force-pushes still fire."""
        emitter = OccCompanionEmitter()
        action, git_calls = _run_emit(
            emitter, tmp_path, ls_remote_shas=[_BASE_SHA, _BASE_SHA]
        )

        assert action.startswith("authored OCC companion"), action
        assert _ls_remote_calls(git_calls), "freshness check must still run"
        assert len(_push_calls(git_calls)) == 2, (
            f"both force-pushes must fire on the unchanged-base path: "
            f"{_push_calls(git_calls)!r}"
        )

    def test_moved_base_unrelated_ticket_does_not_raise(self, tmp_path: Path) -> None:
        """OMN-16116: OCC's default branch moving is NOT itself a collision.

        This is the regression the round-2 narrowing exists to prevent. With
        the naive pre-narrowing predicate (``remote_sha != base_sha`` alone),
        this exact scenario — the remote moved, but only an UNRELATED
        ticket's contract changed — would incorrectly raise
        ``StaleCompanionBaseError`` and abort a run that has no actual
        collision. Confirmed RED/GREEN: temporarily reverting
        ``_assert_base_still_fresh`` to the naive SHA-only check (dropping
        the scoped-diff narrowing) makes this test fail with
        ``StaleCompanionBaseError`` raised where none is expected; restoring
        the narrowed check makes it pass.
        """
        emitter = OccCompanionEmitter()
        action, git_calls = _run_emit(
            emitter,
            tmp_path,
            ls_remote_shas=[_MOVED_SHA, _MOVED_SHA],
            diff_paths=_OTHER_TICKET_DIFF,
        )

        assert action.startswith("authored OCC companion"), action
        assert _ls_remote_calls(git_calls), "freshness check must still run"
        fetch_calls = [c for c in git_calls if "fetch" in c]
        diff_calls = [c for c in git_calls if "diff" in c and "--name-only" in c]
        assert fetch_calls, "a moved remote must trigger the scoped-diff fetch"
        assert diff_calls, "a moved remote must trigger the scoped diff"
        assert len(_push_calls(git_calls)) == 2, (
            "an unrelated-ticket move on OCC's default branch must NOT abort "
            f"the run — both force-pushes must still fire: {_push_calls(git_calls)!r}"
        )

    def test_lease_released_on_stale_base_abort(self, tmp_path: Path) -> None:
        """The OMN-14793 lease's ``finally`` must still release on this new
        fail-fast path, exactly as it does for any other mid-mint exception."""
        emitter = OccCompanionEmitter()
        release_mock = MagicMock()
        with pytest.raises(StaleCompanionBaseError):
            _run_emit(
                emitter,
                tmp_path,
                ls_remote_shas=[_MOVED_SHA],
                lease_release=release_mock,
            )
        release_mock.assert_called_once()
