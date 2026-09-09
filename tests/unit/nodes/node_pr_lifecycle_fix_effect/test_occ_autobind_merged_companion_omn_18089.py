# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18089: the autobind producer must not displace a MERGED companion.

Live incident, 2026-09-09, ``omninode_infra#1284``:

* ``OCC#8816`` was hand-authored by the union-resolve path on branch
  ``jonah/omn-18085-omnidash-companion`` and MERGED at 19:14:49Z. It carries
  ``drift/dod_receipts/OMN-18085/dod-OmniNode-ai-omninode_infra-pr-1284/command.yaml``
  — a receipt id that encodes this exact ``(repo, pr_number)``.
* The required companion-merged gate passed against it at 19:30:56Z.
* At 19:31:03Z ``node_occ_companion_effect`` declined a mint with
  ``already_bound``. Twenty-one seconds later this producer minted ``OCC#8825``
  anyway and PATCHed the product PR body onto it, because the OMN-16386
  binding-identity check accepts a cited companion ONLY when its head branch
  equals the deterministic autobind branch. A hand-authored companion never
  matches that shape, so a genuine, merged, correct binding was classified as
  the cascade-template class.
* ``#1284`` then merged at 19:37:42Z carrying a citable stamp naming a
  companion that is now CLOSED.

Two guards close it, and both are asserted here:

AC1  ``_occ_binding_matches_this_pr`` accepts a cited companion whose changed
     files carry a receipt directory encoding this exact ``(repo, pr_number)``,
     whatever its branch is named — while a cascade-template companion, whose
     receipts encode a DIFFERENT product PR, still fails and still mints.

AC2  ``_patch_evidence_source`` never replaces an existing evidence-source line
     that names a MERGED companion. It fails closed: no PATCH, and a marked
     note on the product PR so the divergence is visible instead of silent.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    OccCompanionEmitter,
)

_MOD = "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter"

# The live incident's coordinates, used verbatim so the regression reads as the
# incident it encodes.
_PRODUCT_REPO = "OmniNode-ai/omninode_infra"
_PRODUCT_PR = 1284
_MERGED_OCC = 8816
_MERGED_OCC_BRANCH = "jonah/omn-18085-omnidash-companion"
_DISPLACING_OCC = 8825
_TICKET = "OMN-18085"

_BOUND_RECEIPT_PATH = (
    f"drift/dod_receipts/{_TICKET}/"
    f"dod-OmniNode-ai-omninode_infra-pr-{_PRODUCT_PR}/command.yaml"
)


def _product_pr_payload(body: str) -> dict[str, object]:
    return {
        "body": body,
        "title": f"fix({_TICKET}): omnidash rolls one pod at a time",
        "head": {"sha": "d" * 40, "ref": "jonah/omn-18085-omnidash-rollout"},
        "state": "open",
        "base": {"repo": {"private": True}},
    }


def _bound_body(occ_pr_number: int) -> str:
    return (
        "Restores single-pod rollout.\n\n"
        f"Evidence-Ticket: {_TICKET}\n"
        f"Evidence-Source: OCC#{occ_pr_number}\n"
    )


# ---------------------------------------------------------------------------
# AC1 — a merged, receipt-proven companion on a non-autobind branch binds
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandAuthoredCompanionIsAGenuineBinding:
    def test_merged_hand_authored_companion_is_a_noop_not_a_second_mint(
        self,
    ) -> None:
        """AC1: the exact live shape must no-op.

        ``OCC#8816``'s branch is not the autobind shape, so the branch leg
        fails — but its changed files carry this product PR's own receipt id,
        which is mechanical proof it was minted for THIS PR. The producer must
        honour that and open nothing.
        """
        emitter = OccCompanionEmitter()

        def fake_rest(method: str, path: str, *, body=None, token=None) -> dict:
            if path.endswith(f"/pulls/{_PRODUCT_PR}"):
                return _product_pr_payload(_bound_body(_MERGED_OCC))
            if path.endswith(f"/pulls/{_MERGED_OCC}"):
                return {"head": {"ref": _MERGED_OCC_BRANCH}}
            raise AssertionError(f"unexpected rest_json call: {method} {path}")

        def fake_rest_array(method: str, path: str, *, token=None) -> list:
            if f"/pulls/{_MERGED_OCC}/files" in path and "page=1" in path:
                return [{"filename": _BOUND_RECEIPT_PATH}]
            if "/files" in path:
                return []
            raise AssertionError(f"unexpected rest_json_array call: {method} {path}")

        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
            patch(f"{_MOD}.rest_json_array", side_effect=fake_rest_array),
            patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
        ):
            result = emitter._emit_companion_sync(_PRODUCT_REPO, _PRODUCT_PR, None)

        assert "no-op" in result
        assert f"OCC#{_MERGED_OCC}" in result

    def test_cascade_template_stamp_still_mints(self) -> None:
        """AC1 negative: OMN-16386's protection survives the widening.

        A cascade-template PR inherits a stamp naming a companion minted for a
        DIFFERENT product PR. That companion's branch encodes the other PR and
        so do its receipts, so neither leg matches and the producer still falls
        through to a fresh mint.
        """
        emitter = OccCompanionEmitter()
        template_occ = 6838

        def fake_rest(method: str, path: str, *, body=None, token=None) -> dict:
            if path.endswith("/pulls/6850"):
                return {
                    "body": (
                        "context\n\nEvidence-Source: OCC#6838\n"
                        "Evidence-Ticket: OMN-16322\n"
                    ),
                    "title": "chore(OMN-16322): bump omnibase_core",
                    "head": {"sha": "a" * 40, "ref": "release/bump"},
                    "state": "open",
                }
            if path.endswith(f"/pulls/{template_occ}"):
                return {
                    "head": {
                        "ref": "auto/omninode-ai-omnibase_core-pr-1575-occ-autobind"
                    }
                }
            # Courtesy comments and any other write are irrelevant here — the
            # assertion is only that the run does NOT take the bound no-op.
            return {}

        def fake_rest_array(method: str, path: str, *, token=None) -> list:
            if f"/pulls/{template_occ}/files" in path and "page=1" in path:
                # Receipts encode omnibase_core#1575, never this PR.
                return [
                    {
                        "filename": (
                            "drift/dod_receipts/OMN-16322/"
                            "dod-OmniNode-ai-omnibase_core-pr-1575/command.yaml"
                        )
                    }
                ]
            return []

        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
            patch(f"{_MOD}.rest_json_array", side_effect=fake_rest_array),
            patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
            patch(f"{_MOD}.find_open_companions", return_value=()),
            patch(f"{_MOD}.acquire_occ_companion_lease", return_value=False),
        ):
            result = emitter._emit_companion_sync(
                "OmniNode-ai/onex_change_control", 6850, None
            )

        assert "no-op" not in result
        assert "already bound" not in result


# ---------------------------------------------------------------------------
# AC2 — the stamp writer never overwrites a merged companion's stamp
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStampWriterRefusesToDisplaceAMergedCompanion:
    def test_patch_is_refused_when_existing_stamp_names_a_merged_companion(
        self,
    ) -> None:
        """AC2: no PATCH, and a marked note explaining the refusal.

        This is the write that made the live incident permanent: the product PR
        merged six minutes later carrying the displaced stamp.
        """
        emitter = OccCompanionEmitter()
        posted: list[dict[str, object]] = []
        patched = False

        def fake_rest(method: str, path: str, *, body=None, token=None) -> dict:
            nonlocal patched
            if method == "GET" and path.endswith(f"/pulls/{_MERGED_OCC}"):
                return {
                    "number": _MERGED_OCC,
                    "state": "closed",
                    "merged_at": "2026-09-09T19:14:49Z",
                }
            if method == "PATCH":
                patched = True
                return {}
            if method == "POST" and path.endswith("/comments"):
                posted.append({"path": path, "body": body})
                return {}
            raise AssertionError(f"unexpected rest_json call: {method} {path}")

        def fake_rest_array(method: str, path: str, *, token=None) -> list:
            if "/comments" in path:
                return []
            raise AssertionError(f"unexpected rest_json_array call: {method} {path}")

        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
            patch(f"{_MOD}.rest_json_array", side_effect=fake_rest_array),
            patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
        ):
            emitter._patch_evidence_source(
                repo=_PRODUCT_REPO,
                pr_number=_PRODUCT_PR,
                occ_pr_number=_DISPLACING_OCC,
                tickets=[_TICKET],
                existing_body=_bound_body(_MERGED_OCC),
            )

        assert patched is False, "a merged companion's stamp must never be overwritten"
        assert len(posted) == 1, "the refusal must be visible on the product PR"
        note = str(posted[0]["body"]["body"])  # type: ignore[index]
        assert f"OCC#{_MERGED_OCC}" in note
        assert f"OCC#{_DISPLACING_OCC}" in note

    def test_patch_proceeds_when_existing_stamp_names_an_unmerged_companion(
        self,
    ) -> None:
        """AC2 true-positive: repair of a stale, never-merged stamp still works.

        The guard is scoped to MERGED companions. A stamp naming a closed or
        open companion is exactly the repair case the producer exists for, and
        must still be rewritten.
        """
        emitter = OccCompanionEmitter()
        patched = False

        def fake_rest(method: str, path: str, *, body=None, token=None) -> dict:
            nonlocal patched
            if method == "GET" and path.endswith(f"/pulls/{_MERGED_OCC}"):
                return {
                    "number": _MERGED_OCC,
                    "state": "closed",
                    "merged_at": None,
                }
            if method == "PATCH":
                patched = True
                return {}
            raise AssertionError(f"unexpected rest_json call: {method} {path}")

        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
            patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
        ):
            emitter._patch_evidence_source(
                repo=_PRODUCT_REPO,
                pr_number=_PRODUCT_PR,
                occ_pr_number=_DISPLACING_OCC,
                tickets=[_TICKET],
                existing_body=_bound_body(_MERGED_OCC),
            )

        assert patched is True
