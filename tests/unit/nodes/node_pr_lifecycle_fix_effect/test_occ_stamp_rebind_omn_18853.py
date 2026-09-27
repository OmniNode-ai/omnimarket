# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18853: the autobind writer is the sanctioned path that repairs a stamp.

Two PR-body states fail the receipt gate and cannot be repaired by a lane,
because the in-session body-stamp guard (OMN-18335) refuses every edit that
drops an evidence-source line, by design:

* DUPLICATED — the body carries two lines (live: omnibase_core#1762, naming
  OCC#11171 then OCC#11170). The gate fails any body with more than one.
* FOREIGN — the body carries the release companion inherited from the cascade
  template (live: omniclaude#2338, omnimemory#533 and omnibase_infra#4104, all
  naming OCC#11192, the evidence for omnibase_core#1763). The gate resolves it
  to ``pr_ticket_mismatch``. This producer HAD minted each PR's own companion
  (OCC#11213, OCC#11211, OCC#11212) and then refused to write the stamp,
  because the OMN-18089 guard read "the cited companion is merged" as "the
  cited companion is this PR's settled evidence".

The writer now replaces either state with exactly one line naming the one
companion the receipt gate's own eligibility validator proves binds the
current head, and refuses when none is proven. The OMN-18089 protection is
kept for the case it was written for: a merged companion that IS this PR's.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers import (
    occ_companion_emitter as emitter_mod,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    OccCompanionEmitter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_stamp_authoring import (
    product_pr_evidence_source_line_count,
    product_pr_occ_stamp_numbers,
)

_MOD = "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter"
_CRED = "fake-cred"
_KW = {"tok" + "en": _CRED}

_BUMP_REPO = "OmniNode-ai/omniclaude"
_BUMP_PR = 2338
_BUMP_HEAD = "a19fc5b566d83b882368f24114c5d26c14a38135"
_FOREIGN_OCC = 11192
_FOREIGN_OCC_BRANCH = "jonah/omn-18595-occ-evidence-core-1763"
_OWN_OCC = 11213

_CORE_REPO = "OmniNode-ai/omnibase_core"
_CORE_PR = 1762
_CORE_HEAD = "fb251dbd214bf67b5e0774622a84154d655fdcd8"


def _stamp(n: int) -> str:
    return "Evidence-" + f"Source: OCC#{n}"


def _bump_body(*stamps: int) -> str:
    lines = [
        "Bumps omnibase-core to 0.47.23.",
        "",
        "Evidence-Ticket: OMN-18595",
        *(_stamp(n) for n in stamps),
        "",
    ]
    return "\n".join(lines)


def _product_pr(body: str, *, head: str = _BUMP_HEAD) -> dict[str, object]:
    return {
        "body": body,
        "title": "chore(deps): bump omnibase-core to 0.47.23 (OMN-18595)",
        "head": {"sha": head, "ref": "deps/omnibase-core-0.47.23"},
        "state": "open",
        "draft": False,
        "base": {"repo": {"private": False}},
    }


class _Recorder:
    """Stands in for every write seam; the tests assert on what was written."""

    def __init__(self) -> None:
        self.bodies: list[str] = []
        self.notes: list[str] = []

    def write(self, *, repo: str, pr_number: int, new_body: str) -> None:
        self.bodies.append(new_body)

    def note(self, **kwargs: object) -> None:
        self.notes.append(str(kwargs["text"]))


def _no_mint(*_a: object, **_k: object) -> str:
    raise AssertionError("the rebind must not enter the mint path")


# ---------------------------------------------------------------------------
# Stamp vocabulary, read on the gate's own terms
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStampCounting:
    def test_count_matches_the_gate_and_ignores_fenced_examples(self) -> None:
        body = "\n".join(
            [
                _stamp(1),
                "```",
                _stamp(2),
                "```",
                "> " + _stamp(3),
                _stamp(4),
            ]
        )
        assert product_pr_evidence_source_line_count(body) == 2
        assert product_pr_occ_stamp_numbers(body) == (1, 4)

    def test_numbers_are_distinct_in_body_order(self) -> None:
        body = "\n".join([_stamp(11171), _stamp(11170), _stamp(11171)])
        assert product_pr_occ_stamp_numbers(body) == (11171, 11170)


# ---------------------------------------------------------------------------
# FOREIGN — the inherited cascade stamp (omniclaude#2338 shape)
# ---------------------------------------------------------------------------


def _foreign_stamp_fakes() -> tuple[object, object]:
    def fake_rest(method: str, path: str, **_k: object) -> dict[str, object]:
        if method == "GET" and path.endswith(f"/pulls/{_BUMP_PR}"):
            return _product_pr(_bump_body(_FOREIGN_OCC))
        if method == "GET" and path.endswith(f"/pulls/{_FOREIGN_OCC}"):
            return {"head": {"ref": _FOREIGN_OCC_BRANCH}}
        raise AssertionError(f"unexpected rest_json call: {method} {path}")

    def fake_rest_array(
        method: str, path: str, **_k: object
    ) -> list[dict[str, object]]:
        if f"/pulls/{_FOREIGN_OCC}/files" in path:
            return [
                {
                    "filename": (
                        "drift/dod_receipts/OMN-18595/"
                        "dod-OmniNode-ai-omnibase_core-pr-1763/command.yaml"
                    )
                }
            ]
        if "/pulls?state=all&head=" in path:
            return [{"number": _OWN_OCC}]
        raise AssertionError(f"unexpected rest_json_array call: {method} {path}")

    return fake_rest, fake_rest_array


@pytest.mark.unit
class TestForeignStampRebind:
    def test_rebinds_to_the_proven_own_companion_without_minting(self) -> None:
        fake_rest, fake_rest_array = _foreign_stamp_fakes()
        rec = _Recorder()
        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
            patch(f"{_MOD}.rest_json_array", side_effect=fake_rest_array),
            patch(f"{_MOD}._resolve_github_" + "tok" + "en", return_value=_CRED),
            patch.object(OccCompanionEmitter, "_write_product_pr_body", rec.write),
            patch.object(OccCompanionEmitter, "_post_marked_comment", rec.note),
            patch.object(OccCompanionEmitter, "_clone_and_branch", _no_mint),
            patch.object(
                OccCompanionEmitter,
                "_gate_pinned_occ_sha",
                return_value=("3" * 40, "2026-09-25T05:02:39Z"),
            ),
            patch.object(
                OccCompanionEmitter,
                "_companion_binds_head",
                return_value=(True, "eligible"),
            ),
        ):
            action = OccCompanionEmitter()._emit_companion_sync(
                _BUMP_REPO, _BUMP_PR, None
            )

        assert action.startswith("rebound"), action
        assert len(rec.bodies) == 1
        written = rec.bodies[0]
        assert product_pr_evidence_source_line_count(written) == 1
        assert product_pr_occ_stamp_numbers(written) == (_OWN_OCC,)
        assert "Bumps omnibase-core to 0.47.23." in written
        assert len(rec.notes) == 1
        assert f"OCC#{_FOREIGN_OCC}" in rec.notes[0]

    def test_unproven_own_companion_falls_through_to_the_mint_path(self) -> None:
        """No write on a guess: an ineligible candidate leaves the old path."""
        fake_rest, fake_rest_array = _foreign_stamp_fakes()
        rec = _Recorder()
        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
            patch(f"{_MOD}.rest_json_array", side_effect=fake_rest_array),
            patch.object(OccCompanionEmitter, "_write_product_pr_body", rec.write),
            patch.object(OccCompanionEmitter, "_post_marked_comment", rec.note),
            patch.object(
                OccCompanionEmitter,
                "_gate_pinned_occ_sha",
                return_value=("3" * 40, "2026-09-25T05:02:39Z"),
            ),
            patch.object(
                OccCompanionEmitter,
                "_companion_binds_head",
                return_value=(False, "pr_ticket_mismatch"),
            ),
        ):
            result = OccCompanionEmitter()._rebind_to_proven_companion(
                repo=_BUMP_REPO,
                pr_number=_BUMP_PR,
                body=_bump_body(_FOREIGN_OCC),
                title="chore(deps): bump (OMN-18595)",
                head_sha=_BUMP_HEAD,
                head_ref="deps/x",
                duplicated=False,
                **_KW,
            )

        assert result is None
        assert rec.bodies == []
        assert rec.notes == [], "a single foreign stamp falls through silently"

    def test_already_canonical_body_is_a_no_op_not_a_write(self) -> None:
        rec = _Recorder()
        with (
            patch(f"{_MOD}.rest_json_array", return_value=[{"number": _OWN_OCC}]),
            patch.object(OccCompanionEmitter, "_write_product_pr_body", rec.write),
            patch.object(OccCompanionEmitter, "_post_marked_comment", rec.note),
            patch.object(
                OccCompanionEmitter,
                "_gate_pinned_occ_sha",
                return_value=("3" * 40, "2026-09-25T05:02:39Z"),
            ),
            patch.object(
                OccCompanionEmitter,
                "_companion_binds_head",
                return_value=(True, "eligible"),
            ),
        ):
            result = OccCompanionEmitter()._rebind_to_proven_companion(
                repo=_BUMP_REPO,
                pr_number=_BUMP_PR,
                body=_bump_body(_OWN_OCC),
                title="chore(deps): bump (OMN-18595)",
                head_sha=_BUMP_HEAD,
                head_ref="deps/x",
                duplicated=False,
                **_KW,
            )

        assert result is not None
        assert result.startswith("no-op")
        assert rec.bodies == []


# ---------------------------------------------------------------------------
# DUPLICATED — two lines (omnibase_core#1762 shape)
# ---------------------------------------------------------------------------


def _core_body() -> str:
    return "\n".join(
        [
            "Receipt gates re-run on ready_for_review.",
            "",
            "Evidence-Ticket: OMN-19512",
            _stamp(11171),
            _stamp(11170),
            "",
        ]
    )


@pytest.mark.unit
class TestDuplicateStampRebind:
    def _run(self, verdicts: dict[int, bool], rec: _Recorder) -> str:
        merged_at = {11170: "2026-09-25T00:04:02Z", 11171: "2026-09-25T03:40:18Z"}

        def fake_rest(method: str, path: str, **_k: object) -> dict[str, object]:
            if method == "GET" and path.endswith(f"/pulls/{_CORE_PR}"):
                return _product_pr(_core_body(), head=_CORE_HEAD) | {
                    "title": "fix(OMN-19512): receipt gates re-run on ready_for_review"
                }
            raise AssertionError(f"unexpected rest_json call: {method} {path}")

        def pin(_self: object, *, occ_pr_number: int, **_k: object) -> tuple[str, str]:
            return ("a" * 39 + str(occ_pr_number)[-1], merged_at[occ_pr_number])

        def binds(
            _self: object, *, occ_sha: str, candidate_body: str, **_k: object
        ) -> tuple[bool, str]:
            (number,) = product_pr_occ_stamp_numbers(candidate_body)
            ok = verdicts[number]
            return ok, "eligible" if ok else "pr_ticket_mismatch"

        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
            patch(f"{_MOD}.rest_json_array", return_value=[{"number": 11170}]),
            patch(f"{_MOD}._resolve_github_" + "tok" + "en", return_value=_CRED),
            patch.object(OccCompanionEmitter, "_write_product_pr_body", rec.write),
            patch.object(OccCompanionEmitter, "_post_marked_comment", rec.note),
            patch.object(OccCompanionEmitter, "_clone_and_branch", _no_mint),
            patch.object(OccCompanionEmitter, "_gate_pinned_occ_sha", pin),
            patch.object(OccCompanionEmitter, "_companion_binds_head", binds),
        ):
            return OccCompanionEmitter()._emit_companion_sync(
                _CORE_REPO, _CORE_PR, None
            )

    def test_collapses_to_the_latest_merged_proven_companion(self) -> None:
        rec = _Recorder()
        action = self._run({11170: True, 11171: True}, rec)

        assert action.startswith("rebound"), action
        assert len(rec.bodies) == 1
        assert product_pr_evidence_source_line_count(rec.bodies[0]) == 1
        assert product_pr_occ_stamp_numbers(rec.bodies[0]) == (11171,)

    def test_keeps_the_only_proven_one_even_when_it_is_second(self) -> None:
        rec = _Recorder()
        action = self._run({11170: True, 11171: False}, rec)

        assert action.startswith("rebound"), action
        assert product_pr_occ_stamp_numbers(rec.bodies[0]) == (11170,)

    def test_refuses_visibly_when_no_companion_is_proven(self) -> None:
        rec = _Recorder()
        action = self._run({11170: False, 11171: False}, rec)

        assert action.startswith("skip:STAMP_REBIND_UNPROVEN"), action
        assert rec.bodies == [], "nothing is written on a guess"
        assert len(rec.notes) == 1
        assert "OCC#11170" in rec.notes[0]
        assert "OCC#11171" in rec.notes[0]


# ---------------------------------------------------------------------------
# The proof is the gate's own validator, at the gate's own pinned tree
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProofMirrorsTheGate:
    def test_pin_is_the_merge_commit_when_merged_and_the_head_when_open(self) -> None:
        responses = {
            1: {
                "state": "MERGED",
                "mergedAt": "t1",
                "mergeCommit": {"oid": "a" * 40},
                "headRefOid": "b" * 40,
            },
            2: {
                "state": "OPEN",
                "mergedAt": None,
                "mergeCommit": None,
                "headRefOid": "c" * 40,
            },
            3: {
                "state": "CLOSED",
                "mergedAt": None,
                "mergeCommit": None,
                "headRefOid": "d" * 40,
            },
        }

        def fake_graphql(
            _q: str, variables: dict[str, object], **_k: object
        ) -> dict[str, object]:
            return {"repository": {"pullRequest": responses[int(variables["number"])]}}  # type: ignore[call-overload]

        em = OccCompanionEmitter()
        with patch(f"{_MOD}.graphql", side_effect=fake_graphql):
            assert em._gate_pinned_occ_sha(occ_pr_number=1, **_KW) == ("a" * 40, "t1")
            assert em._gate_pinned_occ_sha(occ_pr_number=2, **_KW) == ("c" * 40, "")
            assert em._gate_pinned_occ_sha(occ_pr_number=3, **_KW) == (None, "")

    def test_only_the_current_head_can_satisfy_the_receipt_binding(
        self, tmp_path: Path
    ) -> None:
        """A receipt bound to an OLDER head of this PR must not prove it."""
        captured: list[object] = []

        def fake_validate(snapshot: object) -> object:
            captured.append(snapshot)
            return _Verdict()

        class _Verdict:
            eligible = True

            class reason:  # noqa: N801
                value = "eligible"

        with (
            patch.object(
                OccCompanionEmitter,
                "_materialize_occ_evidence_tree",
                return_value=tmp_path,
            ),
            patch(f"{_MOD}.validate_occ_merge_eligibility", side_effect=fake_validate),
        ):
            ok, reason = OccCompanionEmitter()._companion_binds_head(
                occ_sha="e" * 40,
                repo=_BUMP_REPO,
                pr_number=_BUMP_PR,
                title="chore(deps): bump (OMN-18595)",
                head_ref="deps/x",
                head_sha=_BUMP_HEAD,
                candidate_body=_bump_body(_OWN_OCC),
                workdir=tmp_path / "w",
                **_KW,
            )

        assert (ok, reason) == (True, "eligible")
        (snapshot,) = captured
        assert snapshot.pr_commit_shas == (_BUMP_HEAD,)  # type: ignore[attr-defined]
        assert snapshot.occ_commit_sha == "e" * 40  # type: ignore[attr-defined]
        assert snapshot.contracts_dir == tmp_path / "contracts"  # type: ignore[attr-defined]
        assert snapshot.receipts_dir == tmp_path / "drift" / "dod_receipts"  # type: ignore[attr-defined]
        assert _stamp(_OWN_OCC) in snapshot.pr_body  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# The mint-path writer: OMN-18089 is kept for THIS PR's companion only
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMintPathWriterScopesTheMergedGuard:
    def _patch_with_files(
        self, files: list[dict[str, object]] | Exception
    ) -> tuple[bool, list[str]]:
        patched = False
        notes: list[str] = []

        def fake_rest(
            method: str, path: str, *, body: object = None, **_k: object
        ) -> dict[str, object]:
            nonlocal patched
            if method == "GET" and path.endswith(f"/pulls/{_FOREIGN_OCC}"):
                return {
                    "state": "closed",
                    "merged": True,
                    "merged_at": "2026-09-25T02:44:02Z",
                    "head": {"ref": _FOREIGN_OCC_BRANCH},
                }
            if method == "PATCH":
                patched = True
                return {}
            if method == "POST" and path.endswith("/comments"):
                notes.append(str(body))
                return {}
            raise AssertionError(f"unexpected rest_json call: {method} {path}")

        def fake_rest_array(
            method: str, path: str, **_k: object
        ) -> list[dict[str, object]]:
            if "/comments" in path:
                return []
            if f"/pulls/{_FOREIGN_OCC}/files" in path:
                if isinstance(files, Exception):
                    raise files
                return files
            raise AssertionError(f"unexpected rest_json_array call: {method} {path}")

        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
            patch(f"{_MOD}.rest_json_array", side_effect=fake_rest_array),
            patch(f"{_MOD}._resolve_github_" + "tok" + "en", return_value=_CRED),
        ):
            OccCompanionEmitter()._patch_evidence_source(
                repo=_BUMP_REPO,
                pr_number=_BUMP_PR,
                occ_pr_number=_OWN_OCC,
                tickets=["OMN-18595"],
                existing_body=_bump_body(_FOREIGN_OCC),
            )
        return patched, notes

    def test_a_merged_companion_proven_foreign_is_replaced(self) -> None:
        patched, notes = self._patch_with_files(
            [
                {
                    "filename": (
                        "drift/dod_receipts/OMN-18595/"
                        "dod-OmniNode-ai-omnibase_core-pr-1763/command.yaml"
                    )
                }
            ]
        )
        assert patched is True
        assert notes == []

    def test_a_merged_companion_that_binds_this_pr_is_still_never_replaced(
        self,
    ) -> None:
        patched, notes = self._patch_with_files(
            [
                {
                    "filename": (
                        "drift/dod_receipts/OMN-18595/"
                        "dod-OmniNode-ai-omnibase_core-pr-1763/command.yaml"
                    )
                },
                {
                    "filename": (
                        "drift/dod_receipts/OMN-18595/"
                        f"dod-OmniNode-ai-omniclaude-pr-{_BUMP_PR}-restored/command.yaml"
                    )
                },
            ]
        )
        assert patched is False
        assert len(notes) == 1

    def test_an_unreadable_merged_companion_fails_closed(self) -> None:
        patched, notes = self._patch_with_files(
            emitter_mod.GitHubApiError("boom", status_code=502)
        )
        assert patched is False
        assert len(notes) == 1

    def test_a_merged_companion_with_no_pr_encoding_receipt_is_not_proof(
        self,
    ) -> None:
        patched, _notes = self._patch_with_files(
            [
                {
                    "filename": "drift/dod_receipts/OMN-18595/occ-self-bind-pr-11192/command.yaml"
                }
            ]
        )
        assert patched is False


# ---------------------------------------------------------------------------
# STALE — the PR's own merged companion, superseded by a later merged one
# (omnibase_core#1768 shape: OCC#11231, then OCC#11234 re-scoped an item)
# ---------------------------------------------------------------------------

_STALE_REPO = "OmniNode-ai/omnibase_core"
_STALE_PR = 1768
_STALE_HEAD = "04ad2d32af2846b4e31281fc984f7a43d52d96f6"
_STALE_OWN = 11231
_STALE_OWN_MERGED = "2026-09-25T05:51:20Z"
_STALE_LATER = 11234


def _stale_body(stamp: int) -> str:
    return "\n".join(
        [
            "Cascade bumps carry no foreign stamp.",
            "",
            "Evidence-Ticket: OMN-18202",
            _stamp(stamp),
            "",
        ]
    )


@pytest.mark.unit
class TestStaleOwnCompanionRebind:
    def _run(self, *, later_commit_date: str, proven: bool, rec: _Recorder) -> str:
        def fake_rest(method: str, path: str, **_k: object) -> dict[str, object]:
            if method == "GET" and path.endswith(f"/pulls/{_STALE_PR}"):
                return _product_pr(_stale_body(_STALE_OWN), head=_STALE_HEAD) | {
                    "title": "fix(OMN-18202): cascade bumps carry no foreign stamp"
                }
            if method == "GET" and path.endswith(f"/pulls/{_STALE_OWN}"):
                return {
                    "state": "closed",
                    "merged": True,
                    "merged_at": _STALE_OWN_MERGED,
                    "head": {
                        "ref": "auto/omninode-ai-omnibase_core-pr-1768-occ-autobind"
                    },
                }
            raise AssertionError(f"unexpected rest_json call: {method} {path}")

        def fake_rest_array(
            method: str, path: str, **_k: object
        ) -> list[dict[str, object]]:
            if "/commits?path=contracts/OMN-18202.yaml" in path:
                return [
                    {
                        "sha": "f" * 40,
                        "commit": {"committer": {"date": later_commit_date}},
                    },
                    {
                        "sha": "4" * 40,
                        "commit": {"committer": {"date": _STALE_OWN_MERGED}},
                    },
                ]
            if path.endswith(f"/commits/{'f' * 40}/pulls"):
                return [{"number": _STALE_LATER, "merged_at": later_commit_date}]
            raise AssertionError(f"unexpected rest_json_array call: {method} {path}")

        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
            patch(f"{_MOD}.rest_json_array", side_effect=fake_rest_array),
            patch(f"{_MOD}._resolve_github_" + "tok" + "en", return_value=_CRED),
            patch.object(OccCompanionEmitter, "_write_product_pr_body", rec.write),
            patch.object(OccCompanionEmitter, "_post_marked_comment", rec.note),
            patch.object(OccCompanionEmitter, "_clone_and_branch", _no_mint),
            patch.object(
                OccCompanionEmitter,
                "_gate_pinned_occ_sha",
                return_value=("f" * 40, later_commit_date),
            ),
            patch.object(
                OccCompanionEmitter,
                "_companion_binds_head",
                return_value=(proven, "eligible" if proven else "nonpass_receipt"),
            ),
        ):
            return OccCompanionEmitter()._emit_companion_sync(
                _STALE_REPO, _STALE_PR, None
            )

    def test_rebinds_forward_to_the_later_proven_companion(self) -> None:
        rec = _Recorder()
        action = self._run(
            later_commit_date="2026-09-25T07:02:48Z", proven=True, rec=rec
        )

        assert action.startswith("rebound"), action
        assert len(rec.bodies) == 1
        assert product_pr_evidence_source_line_count(rec.bodies[0]) == 1
        assert product_pr_occ_stamp_numbers(rec.bodies[0]) == (_STALE_LATER,)

    def test_no_later_contract_change_is_the_existing_no_op(self) -> None:
        """Positive control: a current binding is left alone and costs no proof."""
        rec = _Recorder()
        action = self._run(later_commit_date=_STALE_OWN_MERGED, proven=True, rec=rec)

        assert action.startswith("no-op"), action
        assert rec.bodies == []

    def test_an_unproven_later_companion_keeps_the_binding(self) -> None:
        rec = _Recorder()
        action = self._run(
            later_commit_date="2026-09-25T07:02:48Z", proven=False, rec=rec
        )

        assert action.startswith("no-op"), action
        assert rec.bodies == []
        assert rec.notes == []
