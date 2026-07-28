# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15300: rendered observation records must be yamlfmt-clean on arrival.

These files are committed into ``onex_change_control``, whose pre-commit
``yamlfmt`` hook fails any file it would rewrite. Every observation PR opened
before this fix failed ``Pre-commit`` with::

    yamlfmt...........................................Failed
    - hook id: yamlfmt
    - files were modified by this hook

which took ``CI Summary`` — a required check on OCC ``dev`` — down with it. That
blocker is independent of the ticket-binding defect: fixing only the PR title
would have left every observation PR red.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from omnimarket.events.occ_autoauthor import ModelOccAutoauthorObservation
from omnimarket.events.occ_observation_record import ModelOccObservationRecord
from omnimarket.events.occ_observation_store import (
    YAMLFMT_MAX_LINE_LENGTH,
    parse_occ_observation_record,
    render_occ_observation_record,
)

# Copied from onex_change_control/.yamlfmt (read 2026-07-28). It lives in the
# destination repo, so this test cannot read the real file; a change there that
# is not mirrored here silently weakens this test. The two settings that
# actually bite are include_document_start and max_line_length.
OCC_YAMLFMT_CONFIG = """\
formatter:
  retain_line_breaks: true
  max_line_length: 100
  indent: 2
  include_document_start: true
  pad_line_comments: 2
"""

_yamlfmt = shutil.which("yamlfmt")
requires_yamlfmt = pytest.mark.skipif(
    _yamlfmt is None,
    reason="yamlfmt binary not installed; the structural invariants still run",
)


def _record() -> ModelOccObservationRecord:
    """A record with the long ``reason`` prose that exposed the wrap mismatch.

    Copied from the observation OCC#5245 carried, so the fixture is the payload
    shape that actually failed rather than a short synthetic one that would wrap
    identically under any width.
    """
    return ModelOccObservationRecord(
        product_repo="OmniNode-ai/omnimarket",
        product_pr_number=1922,
        head_sha="cac88675d0419cb9ac4e102270dc9c43954ab613",
        policy_version="v1",
        workflow_run_id=30328534324,
        run_attempt=1,
        recorded_at="2026-07-28T04:22:45Z",
        observation=ModelOccAutoauthorObservation(
            product_repo="OmniNode-ai/omnimarket",
            product_pr_number=1922,
            occ_pr_number=5161,
            minted_by_node=True,
            attestation_match=False,
            occ_preflight_eligible=True,
            observed_at="2026-07-28T04:22:38.051102+00:00",
            reason=(
                "REJECTED: observed companion files are not reproducible from "
                "compute_companion_plan for OmniNode-ai/omnimarket#1922 "
                "(observed_digest='7101f25082ddd6127360aee3f7f396de320f9000e973ee"
                "644656edb91de8a0ef' != expected_digest='5335d5aabcce56233b8ef751"
                "b3b417bcd703f507d6ac7a7450a4d858f9c85ca3'). A hand-authored, "
                "tampered, or stale companion never byte-matches the canonical "
                "COMPUTE output; rerun the producer to regenerate it."
            ),
        ),
    )


def _yamlfmt_roundtrip(text: str, tmp_path: Path) -> str:
    """Return what yamlfmt would write for ``text`` under OCC's config."""
    config = tmp_path / ".yamlfmt"
    config.write_text(OCC_YAMLFMT_CONFIG, encoding="utf-8")
    target = tmp_path / "record.yaml"
    target.write_text(text, encoding="utf-8")
    assert _yamlfmt is not None
    subprocess.run(
        [_yamlfmt, "-conf", str(config), str(target)],
        check=True,
        capture_output=True,
        text=True,
    )
    return target.read_text(encoding="utf-8")


class TestRenderIsYamlfmtStable:
    @requires_yamlfmt
    def test_render_is_yamlfmt_stable(self, tmp_path: Path) -> None:
        """GREEN: the real binary rewrites nothing — the hook stays green."""
        rendered = render_occ_observation_record(_record())
        assert _yamlfmt_roundtrip(rendered, tmp_path) == rendered

    @requires_yamlfmt
    def test_pre_fix_render_was_rewritten(self, tmp_path: Path) -> None:
        """RED: the settings this fix added are load-bearing, not decoration.

        Reproduces the old call — PyYAML's defaults, no document start, width
        80 — and asserts yamlfmt DOES rewrite it. Without this the green test
        above could pass for reasons unrelated to the change.
        """
        payload = {
            "schema_version": "1.0.0",
            **_record().model_dump(mode="json"),
        }
        pre_fix = yaml.safe_dump(payload, sort_keys=True, default_flow_style=False)
        assert _yamlfmt_roundtrip(pre_fix, tmp_path) != pre_fix


class TestAlwaysOnRatchet:
    """Byte-exact guard that runs where the yamlfmt binary is unavailable.

    The tests above are authoritative but binary-gated, and a check that
    silently skips is a check that does not exist. This pins the rendered bytes
    to a golden that WAS verified stable by the real binary, so any change to
    the renderer's formatting has to come here and be re-verified.

    Note there is deliberately no "every line is <= max_line_length" assertion:
    both PyYAML's ``width`` and yamlfmt's ``max_line_length`` are soft wrap
    hints that leave long unbreakable scalars over the limit, so such an
    assertion would fail on output the real formatter accepts. Agreement
    between the two wrappers is the property that matters, and only the binary
    can prove it.
    """

    GOLDEN = (
        "---\n"
        "head_sha: cac88675d0419cb9ac4e102270dc9c43954ab613\n"
        "observation:\n"
        "  attestation_match: false\n"
        "  minted_by_node: true\n"
        "  observed_at: '2026-07-28T04:22:38.051102+00:00'\n"
        "  occ_pr_number: 5161\n"
        "  occ_preflight_eligible: true\n"
        "  product_pr_number: 1922\n"
        "  product_repo: OmniNode-ai/omnimarket\n"
        "  reason: 'REJECTED: observed companion files are not reproducible from "
        "compute_companion_plan for OmniNode-ai/omnimarket#1922\n"
        "    (observed_digest=''7101f25082ddd6127360aee3f7f396de320f9000e973ee6446"
        "56edb91de8a0ef'' != expected_digest=''5335d5aabcce56233b8ef751b3b417bcd70"
        "3f507d6ac7a7450a4d858f9c85ca3'').\n"
        "    A hand-authored, tampered, or stale companion never byte-matches the "
        "canonical COMPUTE output; rerun\n"
        "    the producer to regenerate it.'\n"
        "policy_version: v1\n"
        "product_pr_number: 1922\n"
        "product_repo: OmniNode-ai/omnimarket\n"
        "recorded_at: '2026-07-28T04:22:45Z'\n"
        "run_attempt: 1\n"
        "schema_version: 1.0.0\n"
        "verification_path: unspecified\n"
        "workflow_run_id: 30328534324\n"
    )

    def test_render_matches_the_yamlfmt_verified_golden(self) -> None:
        assert render_occ_observation_record(_record()) == self.GOLDEN

    def test_golden_carries_the_document_start(self) -> None:
        """The single exact property yamlfmt's config demands."""
        assert self.GOLDEN.startswith("---\n")
        assert YAMLFMT_MAX_LINE_LENGTH == 100


class TestRoundTripSurvives:
    def test_parse_is_still_the_exact_inverse(self) -> None:
        record = _record()
        assert parse_occ_observation_record(render_occ_observation_record(record)) == (
            record
        )
