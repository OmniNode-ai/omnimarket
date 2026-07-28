# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15102: prove `handle()` actually WRITES a NEW record in mode="mutate"
and that the write reads back correctly — the one residual gap the A4
activation (OMN-14904) DoD names as the bar ("a green workflow run is NOT
proof"). The existing suite covers dry_run (no mutation), the append-only
guard called directly (never through `handle()`), and the idempotent no-op
case (record already present — `_push`/`_open_or_sync_occ_pr` proven NOT
called). None of those exercises the real commit that `_write_sync` produces
for a genuinely NEW record.

This test seeds a real local git repo (no network) standing in for the
`onex_change_control` clone, WITHOUT the record pre-placed, monkeypatches
`_clone_default` to copy that seed into the handler's own temp clone dir, and
monkeypatches `_push` to CAPTURE (not perform) the push while the temp clone
dir still exists — reading back the real HEAD sha, the real
`git diff --name-only`, and the real file content from inside that directory
before the handler's `tempfile.TemporaryDirectory()` context tears it down.
`_open_or_sync_occ_pr` is monkeypatched to a fixed return (no network), same
convention as the idempotent test's `_push`/`_open_or_sync_occ_pr` stubs.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from omnimarket.events.occ_autoauthor import ModelOccAutoauthorObservation
from omnimarket.events.occ_observation_record import ModelOccObservationRecord
from omnimarket.events.occ_observation_store import (
    OCC_OBSERVATION_EVIDENCE_TICKET,
    occ_observation_contract_relpath,
    occ_observation_evidence_item_id,
    occ_observation_receipt_relpath,
    occ_observation_record_relpath,
    render_occ_observation_record,
)
from omnimarket.nodes.node_occ_observation_effect.handlers.handler_occ_observation_effect import (
    HandlerOccObservationEffect,
)
from omnimarket.nodes.node_occ_observation_effect.models.model_occ_observation_effect_request import (
    ModelOccObservationEffectRequest,
)
from tests.unit.nodes.node_occ_observation_effect.conftest import OCC_FIXTURE_ROOT


def _record() -> ModelOccObservationRecord:
    return ModelOccObservationRecord(
        product_repo="OmniNode-ai/omnimarket",
        product_pr_number=1900,
        head_sha="0123456789abcdef0123456789abcdef01234567",
        policy_version="v1",
        workflow_run_id=200,
        run_attempt=1,
        recorded_at="2026-07-25T00:00:00Z",
        observation=ModelOccAutoauthorObservation(
            product_repo="OmniNode-ai/omnimarket",
            product_pr_number=1900,
            occ_pr_number=None,
            minted_by_node=False,
            attestation_match=True,
            occ_preflight_eligible=True,
            observed_at="2026-07-25T00:00:00Z",
            reason="",
        ),
    )


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _seed_empty_repo(tmp_path: Path) -> tuple[Path, str]:
    """A local "clone" that does NOT carry this attempt's record yet.

    It DOES carry the real contract + receipt tree (OMN-15323): the write path
    now authors self-bind evidence into it, and onex_change_control is never
    without those directories."""
    seed_dir = tmp_path / "seed"
    shutil.copytree(OCC_FIXTURE_ROOT, seed_dir)
    _git(seed_dir, "init", "-q")
    _git(seed_dir, "config", "user.name", "test")
    _git(seed_dir, "config", "user.email", "test@omninode.ai")
    _git(seed_dir, "add", "-A")
    _git(
        seed_dir, "commit", "-q", "-m", "OCC fixture base (no observation records yet)"
    )
    base_sha = _git(seed_dir, "rev-parse", "HEAD")
    return seed_dir, base_sha


def _copy_into(source: Path, dest: str) -> None:
    shutil.copytree(source, dest)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_new_record_mutate_write_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record()
    relpath = occ_observation_record_relpath(record)
    expected_content = render_occ_observation_record(record)

    seed_dir, base_sha = _seed_empty_repo(tmp_path)
    assert not (seed_dir / relpath).exists(), "precondition: record must be NEW"

    handler = HandlerOccObservationEffect()
    monkeypatch.setattr(
        "omnimarket.nodes.node_occ_observation_effect.handlers.handler_occ_observation_effect._resolve_github_token",
        lambda: "dummy-token",
    )
    # OMN-15300: the write path now probes for an open sibling PR before
    # cloning. No sibling exists in this scenario.
    monkeypatch.setattr(handler, "_open_pr_for_identity", lambda *_a, **_k: None)

    def _clone_stub(clone_dir_arg: str, _token: str, _occ_repo: str) -> str:
        _copy_into(seed_dir, clone_dir_arg)
        return "dev"

    monkeypatch.setattr(handler, "_clone_default", _clone_stub)

    pushes: list[dict[str, object]] = []

    def _capture_push(
        clone_dir_arg: str, _branch: str, _token: str, _occ_repo: str
    ) -> None:
        # Read back BEFORE the tempdir context tears down: this is the exact
        # moment the real `_write_sync` would have pushed a real commit.
        # OMN-15323: two pushes now — the record commit, then the self-bind
        # commit — and each is captured so both trees can be asserted.
        pushes.append(
            {
                "head_sha": handler._head_sha(clone_dir_arg),
                "diff_names": _git(
                    Path(clone_dir_arg), "diff", "--name-only", base_sha, "HEAD"
                ).splitlines(),
                "content": (Path(clone_dir_arg) / relpath).read_text(encoding="utf-8"),
            }
        )

    monkeypatch.setattr(handler, "_push", _capture_push)

    def _rest_json(_method: str, path: str, **_kw: object) -> dict[str, object]:
        """The self-bind probe: read the pushed record commit back."""
        return {"sha": path.rsplit("/", 1)[-1]}

    monkeypatch.setattr(
        "omnimarket.nodes.node_occ_observation_effect.handlers."
        "handler_occ_observation_effect.rest_json",
        _rest_json,
    )

    fixed_pr_number = 4700
    fixed_pr_url = "https://github.com/OmniNode-ai/onex_change_control/pull/4700"
    monkeypatch.setattr(
        handler,
        "_open_or_sync_occ_pr",
        lambda *_args, **_kwargs: (fixed_pr_number, fixed_pr_url),
    )

    request = ModelOccObservationEffectRequest(record=record, mode="mutate")
    result = await handler.handle(request)

    # -- result shape --------------------------------------------------
    assert result.mode == "mutate"
    assert result.already_present is False
    assert result.relpath == relpath
    assert result.occ_pr_number == fixed_pr_number
    assert result.occ_pr_url == fixed_pr_url

    # -- the real write actually happened (read back from real git) ----
    assert pushes, "handler never reached _push — no commit was produced"
    assert len(pushes) == 2, (
        "OMN-15323: the record commit and the self-bind commit are both pushed "
        f"before the PR is opened; saw {len(pushes)} push(es)"
    )
    first, final = pushes
    assert first["head_sha"] != base_sha, (
        "HEAD did not move — no commit was made against the real clone"
    )
    assert first["diff_names"] == [relpath], (
        "the record commit touched something other than exactly the one new "
        f"record path (append-only violation): {first['diff_names']!r}"
    )
    assert first["content"] == expected_content, (
        "committed file content does not byte-match the rendered record"
    )

    ticket = OCC_OBSERVATION_EVIDENCE_TICKET
    item = occ_observation_evidence_item_id(record)
    assert sorted(final["diff_names"]) == sorted(  # type: ignore[arg-type]
        [
            relpath,
            occ_observation_contract_relpath(ticket),
            occ_observation_receipt_relpath(ticket, item),
        ]
    ), (
        "the pushed branch must carry exactly the record plus its self-bind "
        f"evidence: {final['diff_names']!r}"
    )
    assert final["content"] == expected_content, (
        "the self-bind commit must not disturb the record's bytes"
    )
