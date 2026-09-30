# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427 — ``classify_workflow_core_pin_only`` acceptance rules.

Fixture shape is the real bot diff of omniintelligence#963 (OMN-9050): the
omnibase_core checkout ``ref:`` in ``check-handshake.yml`` moves, and the bump
engine replaces the two-line "Pinned to" banner with its one-line
"Auto-bumped by" banner. The refusals are the load-bearing half, including the
real omniclaude#2427 shape, where the same bot moved the OCC checkout ref in
``ci.yml`` to an omnibase_core SHA.
"""

from __future__ import annotations

import pytest

from omnimarket.occ_content_probe import classify_workflow_core_pin_only

pytestmark = pytest.mark.unit

OLD = "330a344cdb9c5dedd04a46dfbb0b63dae0baccfb"
NEW = "ad62c0b928760958fb073a9e32621de53c4a9a1a"
OCC = "1b0d2f0374bca1f39d9bab22a02251208cdce196"
HS = ".github/workflows/check-handshake.yml"
CI = ".github/workflows/ci.yml"

HANDSHAKE_BASE = f"""\
name: handshake
jobs:
  handshake:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout omnibase_core
        uses: actions/checkout@v7
        with:
          repository: OmniNode-ai/omnibase_core
          # Pinned to omnibase_core main as of 2026-03-22.
          # Update by running: gh api repos/OmniNode-ai/omnibase_core/commits/main --jq '.sha'
          ref: {OLD}
          path: omnibase_core

      - name: Verify omnibase_core checkout
        run: |
          # a shell comment inside a block scalar
          test -d omnibase_core
"""

HANDSHAKE_HEAD = f"""\
name: handshake
jobs:
  handshake:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout omnibase_core
        uses: actions/checkout@v7
        with:
          repository: OmniNode-ai/omnibase_core
                    # Auto-bumped by omnibase_core publish-downstream-pin-bump.yml to {NEW[:12]}.
          ref: {NEW}
          path: omnibase_core

      - name: Verify omnibase_core checkout
        run: |
          # a shell comment inside a block scalar
          test -d omnibase_core
"""

CI_BASE = f"""\
jobs:
  occ:
    steps:
      - uses: actions/checkout@v7
        with:
          repository: OmniNode-ai/onex_change_control
          ref: {OCC}  # pragma: allowlist secret
          path: onex_change_control
"""
CI_HEAD = CI_BASE.replace(OCC, NEW)


class TestAccepted:
    def test_the_bot_bump_shape_is_pin_only(self) -> None:
        ok, reason = classify_workflow_core_pin_only(
            (HS,), contents={HS: (HANDSHAKE_HEAD, HANDSHAKE_BASE)}
        )
        assert ok, reason
        assert NEW[:12] in reason

    def test_a_bare_ref_move_without_banner_change_is_pin_only(self) -> None:
        head = HANDSHAKE_BASE.replace(OLD, NEW)
        ok, reason = classify_workflow_core_pin_only(
            (HS,), contents={HS: (head, HANDSHAKE_BASE)}
        )
        assert ok, reason

    def test_a_dash_repository_mapping_is_scoped(self) -> None:
        base = f"""\
steps:
  - repository: OmniNode-ai/omnibase_core
    ref: {OLD}
"""
        ok, reason = classify_workflow_core_pin_only(
            (HS,), contents={HS: (base.replace(OLD, NEW), base)}
        )
        assert ok, reason


class TestRefused:
    def test_the_omniclaude_2427_occ_ref_move_is_refused(self) -> None:
        ok, reason = classify_workflow_core_pin_only(
            (HS, CI),
            contents={
                HS: (HANDSHAKE_HEAD, HANDSHAKE_BASE),
                CI: (CI_HEAD, CI_BASE),
            },
        )
        assert not ok
        assert CI in reason

    def test_a_step_edit_alongside_the_pin_is_refused(self) -> None:
        head = HANDSHAKE_HEAD.replace("test -d omnibase_core", "rm -rf /")
        ok, _ = classify_workflow_core_pin_only(
            (HS,), contents={HS: (head, HANDSHAKE_BASE)}
        )
        assert not ok

    def test_a_non_banner_comment_edit_is_refused(self) -> None:
        head = HANDSHAKE_HEAD.replace(
            "# a shell comment inside a block scalar", "# edited"
        )
        ok, _ = classify_workflow_core_pin_only(
            (HS,), contents={HS: (head, HANDSHAKE_BASE)}
        )
        assert not ok

    def test_a_ref_outside_the_core_mapping_is_refused(self) -> None:
        base = (
            HANDSHAKE_BASE
            + f"""\
      - uses: actions/checkout@v7
        with:
          repository: OmniNode-ai/other
          ref: {OLD}
"""
        )
        head = base.replace(f"ref: {OLD}", f"ref: {NEW}")
        ok, _ = classify_workflow_core_pin_only((HS,), contents={HS: (head, base)})
        assert not ok

    def test_a_ref_after_the_mapping_closes_is_refused(self) -> None:
        base = f"""\
with:
  repository: OmniNode-ai/omnibase_core
env:
  ref: {OLD}
"""
        ok, _ = classify_workflow_core_pin_only(
            (HS,), contents={HS: (base.replace(OLD, NEW), base)}
        )
        assert not ok

    def test_a_non_workflow_path_is_refused(self) -> None:
        ok, _ = classify_workflow_core_pin_only(
            ("scripts/x.yml",), contents={"scripts/x.yml": ("a", "b")}
        )
        assert not ok

    def test_an_unreadable_side_is_refused(self) -> None:
        ok, _ = classify_workflow_core_pin_only(
            (HS,), contents={HS: (HANDSHAKE_HEAD, None)}
        )
        assert not ok

    def test_an_empty_diff_is_refused(self) -> None:
        ok, _ = classify_workflow_core_pin_only((), contents={})
        assert not ok

    def test_no_moved_pin_is_refused(self) -> None:
        ok, _ = classify_workflow_core_pin_only(
            (HS,), contents={HS: (HANDSHAKE_BASE, HANDSHAKE_BASE)}
        )
        assert not ok

    def test_two_head_shas_are_refused(self) -> None:
        other = "b" * 40
        ok, _ = classify_workflow_core_pin_only(
            (HS, CI),
            contents={
                HS: (HANDSHAKE_BASE.replace(OLD, NEW), HANDSHAKE_BASE),
                CI: (HANDSHAKE_BASE.replace(OLD, other), HANDSHAKE_BASE),
            },
        )
        assert not ok

    def test_too_many_files_are_refused(self) -> None:
        paths = tuple(f".github/workflows/w{i}.yml" for i in range(6))
        ok, _ = classify_workflow_core_pin_only(paths, contents={})
        assert not ok


class TestEmitterRouting:
    """The emitter's I/O shell routes an all-workflow diff to this classifier."""

    def _emitter_with(self, files: dict[str, tuple[str | None, str | None]]):  # type: ignore[no-untyped-def]
        from unittest.mock import patch

        from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
            OccCompanionEmitter,
        )

        emitter = OccCompanionEmitter()

        def fake(owner: str, repo: str, path: str, ref: str, token: str) -> str | None:
            head, base = files[path]
            return head if ref == "HEAD" else base

        return emitter, patch.object(emitter, "_content_at_ref", side_effect=fake)

    def test_a_bot_bump_is_classified_pin_only(self) -> None:
        emitter, patcher = self._emitter_with({HS: (HANDSHAKE_HEAD, HANDSHAKE_BASE)})
        with patcher:
            ok, reason = emitter._classify_pin_only_diff(
                owner="OmniNode-ai",
                repo_name="omniintelligence",
                changed_files=(HS,),
                head_ref="HEAD",
                base_ref="BASE",
                token="t",
            )
        assert ok, reason

    def test_the_occ_ref_move_is_refused(self) -> None:
        emitter, patcher = self._emitter_with(
            {HS: (HANDSHAKE_HEAD, HANDSHAKE_BASE), CI: (CI_HEAD, CI_BASE)}
        )
        with patcher:
            ok, _ = emitter._classify_pin_only_diff(
                owner="OmniNode-ai",
                repo_name="omniclaude",
                changed_files=(HS, CI),
                head_ref="HEAD",
                base_ref="BASE",
                token="t",
            )
        assert not ok

    def test_an_unresolvable_merge_base_is_refused(self) -> None:
        emitter, patcher = self._emitter_with({HS: (HANDSHAKE_HEAD, HANDSHAKE_BASE)})
        with patcher:
            ok, _ = emitter._classify_pin_only_diff(
                owner="OmniNode-ai",
                repo_name="omniintelligence",
                changed_files=(HS,),
                head_ref="HEAD",
                base_ref=None,
                token="t",
            )
        assert not ok
