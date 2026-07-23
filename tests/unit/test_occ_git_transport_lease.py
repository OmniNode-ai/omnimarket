# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure-unit tests for the single-producer lease helpers (OMN-14793).

The lease is an atomic create-if-absent on a git-ref in the shared OCC repo — the
only durable surface the two live OccCompanionEmitter instances share. These tests
exercise the transport-level primitives directly against a mocked ``rest_json`` /
``rest_no_content`` so the 201/422 discrimination, TTL steal, fail-closed
propagation, best-effort release, and exact key format are proven without any
network I/O (OMN-14783 rec #2).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from omnimarket.github_api import GitHubApiError
from omnimarket.occ_git_transport import (
    _create_lease_commit,
    _create_lease_ref,
    _lease_is_stale,
    _lease_key,
    _resolve_reusable_tree_sha,
    acquire_occ_companion_lease,
    release_occ_companion_lease,
)

_MOD = "omnimarket.occ_git_transport"
_HEAD = "e" * 40
_REF_FULL = f"refs/occ-companion-leases/omninode-ai-omnimarket-pr-321-{_HEAD}"
_REF_SHORT = f"occ-companion-leases/omninode-ai-omnimarket-pr-321-{_HEAD}"


def _iso(seconds_ago: int) -> str:
    return (datetime.now(tz=UTC) - timedelta(seconds=seconds_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# ---------------------------------------------------------------------------
# _lease_key — exact key format (F-C7)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLeaseKey:
    def test_key_format_from_dashed_slug(self) -> None:
        assert (
            _lease_key("OmniNode-ai-omnimarket", 321, _HEAD)
            == f"omninode-ai-omnimarket-pr-321-{_HEAD}"
        )

    def test_key_format_from_slashed_slug_is_identical(self) -> None:
        # The emitter passes ``owner-repo`` but a caller might pass ``owner/repo``;
        # both normalise to the same key so two hosts contend on ONE ref.
        assert _lease_key("OmniNode-ai/omnimarket", 321, _HEAD) == _lease_key(
            "OmniNode-ai-omnimarket", 321, _HEAD
        )


# ---------------------------------------------------------------------------
# _create_lease_ref — the atomic create-if-absent primitive
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCreateLeaseRef:
    def test_201_returns_true(self) -> None:
        with patch(f"{_MOD}.rest_json", return_value={}):
            assert _create_lease_ref("o", "r", _REF_FULL, "c" * 40, "t") is True

    def test_422_returns_false(self) -> None:
        with patch(
            f"{_MOD}.rest_json",
            side_effect=GitHubApiError("Reference already exists", status_code=422),
        ):
            assert _create_lease_ref("o", "r", _REF_FULL, "c" * 40, "t") is False

    def test_other_status_propagates_fail_closed(self) -> None:
        with (
            patch(
                f"{_MOD}.rest_json",
                side_effect=GitHubApiError("server error", status_code=500),
            ),
            pytest.raises(GitHubApiError),
        ):
            _create_lease_ref("o", "r", _REF_FULL, "c" * 40, "t")


# ---------------------------------------------------------------------------
# _resolve_reusable_tree_sha — reuse default-branch tree (OMN-14981 fix:
# POST .../git/trees 422s "Invalid tree info" for every empty-tree shape)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResolveReusableTreeSha:
    def test_returns_default_branch_tree_sha(self) -> None:
        with patch(
            f"{_MOD}.rest_json",
            side_effect=[
                {"default_branch": "dev"},
                {"commit": {"tree": {"sha": "t" * 40}}},
            ],
        ):
            assert _resolve_reusable_tree_sha("o", "r", "tok") == "t" * 40

    def test_missing_default_branch_raises(self) -> None:
        with (
            patch(f"{_MOD}.rest_json", return_value={}),
            pytest.raises(GitHubApiError),
        ):
            _resolve_reusable_tree_sha("o", "r", "tok")

    def test_missing_tree_sha_raises(self) -> None:
        with (
            patch(
                f"{_MOD}.rest_json",
                side_effect=[{"default_branch": "dev"}, {"commit": {}}],
            ),
            pytest.raises(GitHubApiError),
        ):
            _resolve_reusable_tree_sha("o", "r", "tok")


# ---------------------------------------------------------------------------
# _create_lease_commit — fresh server-timestamped lease commit
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCreateLeaseCommit:
    def test_returns_commit_sha(self) -> None:
        with patch(
            f"{_MOD}.rest_json",
            side_effect=[
                {"default_branch": "dev"},
                {"commit": {"tree": {"sha": "t" * 40}}},
                {"sha": "c" * 40},
            ],
        ):
            sha = _create_lease_commit(
                "o", "r", "tok", producer_id="p", pr_number=1, head_sha=_HEAD
            )
        assert sha == "c" * 40

    def test_missing_tree_sha_raises(self) -> None:
        with (
            patch(
                f"{_MOD}.rest_json",
                side_effect=[{"default_branch": "dev"}, {}, {"sha": "c" * 40}],
            ),
            pytest.raises(GitHubApiError),
        ):
            _create_lease_commit(
                "o", "r", "tok", producer_id="p", pr_number=1, head_sha=_HEAD
            )

    def test_missing_default_branch_raises(self) -> None:
        with (
            patch(f"{_MOD}.rest_json", side_effect=[{}]),
            pytest.raises(GitHubApiError),
        ):
            _create_lease_commit(
                "o", "r", "tok", producer_id="p", pr_number=1, head_sha=_HEAD
            )


# ---------------------------------------------------------------------------
# _lease_is_stale — server-authoritative TTL decision (F-C4)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLeaseIsStale:
    def test_recent_lease_is_not_stale(self) -> None:
        with patch(
            f"{_MOD}.rest_json",
            side_effect=[
                {"object": {"sha": "s" * 40}},
                {"committer": {"date": _iso(10)}},
            ],
        ):
            assert _lease_is_stale("o", "r", _REF_SHORT, "t", 900) is False

    def test_old_lease_is_stale(self) -> None:
        with patch(
            f"{_MOD}.rest_json",
            side_effect=[
                {"object": {"sha": "s" * 40}},
                {"committer": {"date": _iso(4000)}},
            ],
        ):
            assert _lease_is_stale("o", "r", _REF_SHORT, "t", 900) is True

    def test_vanished_ref_is_stealable(self) -> None:
        with patch(
            f"{_MOD}.rest_json",
            side_effect=GitHubApiError("Not Found", status_code=404),
        ):
            assert _lease_is_stale("o", "r", _REF_SHORT, "t", 900) is True

    def test_other_error_propagates(self) -> None:
        with (
            patch(
                f"{_MOD}.rest_json",
                side_effect=GitHubApiError("boom", status_code=500),
            ),
            pytest.raises(GitHubApiError),
        ):
            _lease_is_stale("o", "r", _REF_SHORT, "t", 900)


# ---------------------------------------------------------------------------
# acquire_occ_companion_lease — orchestration (F-C7 + F-C4)
# ---------------------------------------------------------------------------


def _acquire() -> bool:
    return acquire_occ_companion_lease(
        token="tok",
        repo_slug="OmniNode-ai-omnimarket",
        pr_number=321,
        head_sha=_HEAD,
        producer_id="producer-a",
        lease_ttl_seconds=900,
    )


@pytest.mark.unit
class TestAcquireOrchestration:
    def test_first_acquirer_wins_and_uses_head_keyed_ref(self) -> None:
        with (
            patch(f"{_MOD}._create_lease_commit", return_value="c" * 40),
            patch(f"{_MOD}._create_lease_ref", return_value=True) as ref_mock,
            patch(f"{_MOD}._lease_is_stale") as stale_mock,
        ):
            assert _acquire() is True
        stale_mock.assert_not_called()
        # The ref is keyed on PR head SHA, exactly refs/occ-companion-leases/<key>.
        assert ref_mock.call_args.args[2] == _REF_FULL

    def test_live_lease_rejects_second_producer(self) -> None:
        with (
            patch(f"{_MOD}._create_lease_commit", return_value="c" * 40),
            patch(f"{_MOD}._create_lease_ref", return_value=False),
            patch(f"{_MOD}._lease_is_stale", return_value=False),
        ):
            assert _acquire() is False

    def test_stale_lease_is_stolen(self) -> None:
        with (
            patch(f"{_MOD}._create_lease_commit", return_value="c" * 40),
            patch(f"{_MOD}._create_lease_ref", side_effect=[False, True]) as ref_mock,
            patch(f"{_MOD}._lease_is_stale", return_value=True),
            patch(f"{_MOD}.rest_no_content") as del_mock,
        ):
            assert _acquire() is True
        del_mock.assert_called_once()
        assert del_mock.call_args.args[1].endswith(f"/git/refs/{_REF_SHORT}")
        assert ref_mock.call_count == 2  # create attempt (422) + re-create after steal

    def test_lost_steal_race_returns_false(self) -> None:
        with (
            patch(f"{_MOD}._create_lease_commit", return_value="c" * 40),
            patch(f"{_MOD}._create_lease_ref", side_effect=[False, False]),
            patch(f"{_MOD}._lease_is_stale", return_value=True),
            patch(f"{_MOD}.rest_no_content"),
        ):
            assert _acquire() is False

    def test_steal_tolerates_delete_404(self) -> None:
        with (
            patch(f"{_MOD}._create_lease_commit", return_value="c" * 40),
            patch(f"{_MOD}._create_lease_ref", side_effect=[False, True]),
            patch(f"{_MOD}._lease_is_stale", return_value=True),
            patch(
                f"{_MOD}.rest_no_content",
                side_effect=GitHubApiError("gone", status_code=404),
            ),
        ):
            assert _acquire() is True

    def test_steal_propagates_delete_500(self) -> None:
        with (
            patch(f"{_MOD}._create_lease_commit", return_value="c" * 40),
            patch(f"{_MOD}._create_lease_ref", side_effect=[False, True]),
            patch(f"{_MOD}._lease_is_stale", return_value=True),
            patch(
                f"{_MOD}.rest_no_content",
                side_effect=GitHubApiError("boom", status_code=500),
            ),
            pytest.raises(GitHubApiError),
        ):
            _acquire()

    def test_fail_closed_on_ref_create_transport_error(self) -> None:
        with (
            patch(f"{_MOD}._create_lease_commit", return_value="c" * 40),
            patch(
                f"{_MOD}._create_lease_ref",
                side_effect=GitHubApiError("unreachable", status_code=503),
            ),
            pytest.raises(GitHubApiError),
        ):
            _acquire()


# ---------------------------------------------------------------------------
# release_occ_companion_lease — best-effort DELETE (F-C7)
# ---------------------------------------------------------------------------


def _release() -> None:
    release_occ_companion_lease(
        token="tok",
        repo_slug="OmniNode-ai-omnimarket",
        pr_number=321,
        head_sha=_HEAD,
    )


@pytest.mark.unit
class TestReleaseLease:
    def test_deletes_the_head_keyed_ref(self) -> None:
        with patch(f"{_MOD}.rest_no_content") as del_mock:
            _release()
        del_mock.assert_called_once()
        assert del_mock.call_args.args[0] == "DELETE"
        assert del_mock.call_args.args[1].endswith(f"/git/refs/{_REF_SHORT}")

    @pytest.mark.parametrize("status", [404, 422])
    def test_missing_ref_is_swallowed(self, status: int) -> None:
        with patch(
            f"{_MOD}.rest_no_content",
            side_effect=GitHubApiError("gone", status_code=status),
        ):
            _release()  # must not raise

    def test_other_error_is_swallowed_not_masked(self) -> None:
        # release runs inside the mint's finally; it must never raise (masking the
        # real outcome) even on a 5xx — it logs and returns.
        with patch(
            f"{_MOD}.rest_no_content",
            side_effect=GitHubApiError("server error", status_code=500),
        ):
            _release()  # must not raise
