# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The producer honours ``op=regenerate`` without a product push (OMN-19832).

Wave-2 task T10 of the PR landing workflow (epic OMN-19822). The workflow sends
``regenerate`` from COMPANION_OPEN once it has observed the companion
conflicting. The push-driven path re-mints a bound companion only when GitHub
answers ``mergeable: false`` and fails closed on ``null`` (OMN-18856), and
``null`` is what GitHub reports for a while after the change-control base
moves, which is exactly when a regenerate arrives. So a regenerate must re-mint
this PR's own OPEN companion whatever the mergeability reading, under the same
per-ticket or per-head lease held from the clone (the read) to the push and the
stamp (the write), which is P3. A merged or closed companion is never
regenerated. Under ``batch_mode=ticket`` the re-mint is the ticket batch
rebuild.

Each regenerate test here fails against the pre-OMN-19832 emitter, which never
read ``op`` (the parameter did not exist). The derive controls pass on both.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from omnimarket.events.occ_companion import (
    EnumOccBatchMode,
    batch_companion_branch_for,
)
from omnimarket.events.pr_landing_companion import EnumPrLandingCompanionOp
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    OccCompanionEmitter,
)
from tests.unit.nodes.node_pr_lifecycle_fix_effect.test_occ_batch_companion_omn_16336 import (
    _BatchScenario,
)

_MOD = "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter"
_REPO = "OmniNode-ai/omnimarket"
_TICKET = "OMN-19832"
_BOUND_BODY = (
    f"Implements the thing.\n\nEvidence-Source: OCC#55\nEvidence-Ticket: {_TICKET}\n"
)
_DIFF = '{"files":[{"path":"tests/unit/test_thing.py"},{"path":"src/x.py"}]}'

REGENERATE = EnumPrLandingCompanionOp.REGENERATE
DERIVE = EnumPrLandingCompanionOp.DERIVE


class _FakeTempDir:
    def __init__(self, path: Path) -> None:
        self._path = path

    def __enter__(self) -> str:
        return str(self._path)

    def __exit__(self, *_exc: object) -> None:
        return None


def _emit(
    tmp_root: Path,
    *,
    op: EnumPrLandingCompanionOp,
    occ_pr_extra: dict[str, object],
    order: list[str] | None = None,
    lease_ok: bool = True,
) -> str:
    """Drive the real per-PR ``_emit_companion_sync`` against a bound PR.

    The product PR 321 is stamped with OCC#55, whose branch is this PR's own
    autobind branch; ``occ_pr_extra`` sets the companion's state and
    mergeability. ``order`` records the lease, clone, push and stamp calls.
    """
    pr_number = 321
    calls = order if order is not None else []

    def fake_rest(
        method: str, path: str, *, body: object = None, token: object = None
    ) -> dict[str, object]:
        del method, body, token
        if path.endswith(f"/pulls/{pr_number}"):
            return {
                "body": _BOUND_BODY,
                "title": f"feat({_TICKET}): the thing",
                "head": {"sha": "b" * 40, "ref": "feature-branch"},
                "state": "open",
                "draft": False,
                "labels": [],
            }
        if "/pulls/55" in path:
            payload: dict[str, object] = {
                "number": 55,
                "state": "open",
                "head": {
                    "ref": f"auto/omninode-ai-omnimarket-pr-{pr_number}-occ-autobind"
                },
            }
            payload.update(occ_pr_extra)
            return payload
        return {}

    def fake_run_git(argv: list[str], *, cwd: str) -> str:
        del cwd
        if "rev-parse" in argv:
            return "c" * 40
        if "ls-remote" in argv:
            return "0" * 40 + "\tHEAD\n"
        return ""

    def fake_clone(cd: Path, *_a: object) -> str:
        calls.append("clone")
        cd.mkdir(parents=True, exist_ok=True)
        return "0" * 40

    def acquire(**_kw: object) -> bool:
        calls.append("acquire")
        return lease_ok

    def release(**_kw: object) -> None:
        calls.append("release")

    def open_or_sync(**_kw: object) -> int:
        calls.append("push")
        return 55

    def patch_stamp(**_kw: object) -> None:
        calls.append("stamp")

    emitter = OccCompanionEmitter()
    with (
        patch(f"{_MOD}.rest_json", side_effect=fake_rest),
        patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
        patch(f"{_MOD}.acquire_occ_companion_lease", side_effect=acquire),
        patch(f"{_MOD}.release_occ_companion_lease", side_effect=release),
        patch.object(emitter, "_find_contending_companions", return_value=[]),
        patch.object(emitter, "_superseding_companions", return_value=[]),
        patch.object(emitter, "_run_git", side_effect=fake_run_git),
        patch.object(emitter, "_clone_and_branch", side_effect=fake_clone),
        patch.object(emitter, "_open_or_sync_occ_pr", side_effect=open_or_sync),
        patch.object(emitter, "_observe_pr_probe", return_value=(_DIFF, 0)),
        patch.object(
            emitter,
            "_derive_content_bound_check",
            return_value=(
                "grep -c 'def test_thing' tests/unit/test_thing.py",
                "a" * 40,
                1,
                (),
            ),
        ),
        patch.object(emitter, "_patch_evidence_source", side_effect=patch_stamp),
        patch(
            f"{_MOD}.tempfile.TemporaryDirectory", return_value=_FakeTempDir(tmp_root)
        ),
    ):
        return emitter._emit_companion_sync(
            _REPO, pr_number, _TICKET, batch_mode=EnumOccBatchMode.OFF, op=op
        )


@pytest.mark.unit
class TestRegenerateReMintsTheOpenCompanion:
    def test_healthy_open_companion_is_re_minted_on_regenerate(
        self, tmp_path: Path
    ) -> None:
        action = _emit(tmp_path, op=REGENERATE, occ_pr_extra={"mergeable": True})
        assert action.startswith("authored OCC companion Evidence-Source: OCC#55"), (
            action
        )

    def test_derive_on_the_same_companion_still_no_ops(self, tmp_path: Path) -> None:
        """Control: the push-driven path is unchanged."""
        action = _emit(tmp_path, op=DERIVE, occ_pr_extra={"mergeable": True})
        assert action.startswith("no-op:"), action

    def test_indeterminate_mergeability_does_not_block_a_regenerate(
        self, tmp_path: Path
    ) -> None:
        action = _emit(tmp_path, op=REGENERATE, occ_pr_extra={"mergeable": None})
        assert action.startswith("authored OCC companion"), action

    def test_derive_still_fails_closed_on_indeterminate_mergeability(
        self, tmp_path: Path
    ) -> None:
        action = _emit(tmp_path, op=DERIVE, occ_pr_extra={"mergeable": None})
        assert action.startswith("no-op:"), action

    @pytest.mark.parametrize(
        "extra",
        [
            {"state": "closed", "merged": True, "mergeable": False},
            {"state": "closed", "merged": False, "mergeable": True},
        ],
        ids=["merged", "closed-unmerged"],
    )
    def test_a_finished_companion_is_never_regenerated(
        self, tmp_path: Path, extra: dict[str, object]
    ) -> None:
        order: list[str] = []
        action = _emit(tmp_path, op=REGENERATE, occ_pr_extra=extra, order=order)
        assert action.startswith("no-op:"), action
        assert order == [], "a finished companion took the lease or was cloned"


@pytest.mark.unit
class TestRegenerateHoldsTheLeaseFromReadToWrite:
    def test_lease_spans_the_clone_the_push_and_the_stamp(self, tmp_path: Path) -> None:
        order: list[str] = []
        _emit(tmp_path, op=REGENERATE, occ_pr_extra={"mergeable": True}, order=order)
        assert order == ["acquire", "clone", "push", "stamp", "release"], order

    def test_a_held_lease_declines_with_zero_writes(self, tmp_path: Path) -> None:
        order: list[str] = []
        action = _emit(
            tmp_path,
            op=REGENERATE,
            occ_pr_extra={"mergeable": True},
            order=order,
            lease_ok=False,
        )
        assert action.startswith("skip:LEASE_HELD"), action
        assert order == ["acquire"], order


@pytest.mark.unit
def test_regenerate_under_batch_mode_rebuilds_the_ticket_batch(tmp_path: Path) -> None:
    """``batch_mode=ticket``: regenerate is the batch rebuild, one PR, new head."""
    scenario = _BatchScenario(tmp_path)
    emitter = OccCompanionEmitter()
    branch = batch_companion_branch_for("OMN-16336")
    ticket_lease = MagicMock(return_value=True)
    with scenario.patches(emitter):
        scenario.emit(emitter, 101)
        scenario.emit(emitter, 102)
        before = scenario.branch_head(branch)

        # Control: a derive on a healthy bound batch member is a no-op.
        control = scenario.emit(emitter, 102)
        assert control.startswith("no-op:"), control
        assert scenario.branch_head(branch) == before

        scenario.advance_dev_unrelated()
        with patch(f"{_MOD}.acquire_occ_ticket_lease", ticket_lease):
            action = emitter._emit_companion_sync(
                _REPO,
                102,
                "OMN-16336",
                batch_mode=EnumOccBatchMode.TICKET,
                op=REGENERATE,
            )

    assert action.startswith("authored OCC companion Evidence-Source: OCC#55"), action
    assert scenario.branch_head(branch) != before, "the batch was not rebuilt"
    assert len(scenario.pull_posts) == 1, "regenerate opened a second companion"
    assert ticket_lease.call_count == 1
    assert ticket_lease.call_args.kwargs["ticket"] == "OMN-16336"
