# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18592 — the compute-path companion ``summary`` must survive OCC's yamlfmt.

Successor to OMN-14684, which moved the compute contract's ``check_value``
fields onto the fold-proof renderer and left ``summary`` behind as a ``>``
folded scalar carrying an unbounded ``{repo}``/``{pr_number}`` interpolation.
``onex_change_control``'s ``.yamlfmt`` reflows that line, so the companion's own
hosted ``yamlfmt`` pre-commit reports ``files were modified by this hook`` and
the companion cannot merge — which holds the PRODUCT PR behind the OCC Companion
Merged Gate for its full timeout. Reproduced live twice: ``onex_change_control``
#9998 (run ``35225319981``) and #10180 (run ``35323471608``), both autobind
companions for ``OmniNode-ai/omniintelligence``, both repaired by hand.

Every assertion here is driven by the CONSUMING repository's real ``.yamlfmt``
and the real ``yamlfmt`` binary, never by a width this file asserts for itself:
narrowing ``onex_change_control/.yamlfmt`` must turn these RED rather than leave
them silently passing on a stale assumption (OMN-18592 AC5). The vendored
``tests/fixtures/occ_companion_golden/occ.yamlfmt`` copy is exercised too, so the
property still has force in CI where no OCC clone exists, and
:func:`test_the_vendored_occ_yamlfmt_fixture_matches_the_real_one` fails if the
copy drifts from the original.
"""

from __future__ import annotations

import difflib
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    compute_contract_summary,
    render_companion_contract,
    render_compute_companion_contract,
)

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "occ_companion_golden"
_VENDORED_OCC_YAMLFMT = _FIXTURES / "occ.yamlfmt"

# The longest ``OmniNode-ai/<repo>`` slug in the registry, at a five-digit PR
# number — the AC1 falsifier's input. onex_change_control passed 10000 PRs on
# 2026-09-17, so five digits is the live width, not a hypothetical one.
_LONGEST_REPO = "OmniNode-ai/onex_change_control"
_FIVE_DIGIT_PR = 99999

# The exact second live recurrence: OCC#10180, the autobind companion for
# OmniNode-ai/omniintelligence#916 (ticket OMN-18669), hand-repaired by commit
# 73824678a0b7992ccfdd4460bfd22f9d3e6f60f3.
_OMN_18669_REPO = "OmniNode-ai/omniintelligence"
_OMN_18669_PR = 916

# Every public registry slug an autobind companion is minted for.
_REGISTRY_SLUGS = (
    "OmniNode-ai/omniweb",
    "OmniNode-ai/omnidash",
    "OmniNode-ai/omnimemory",
    "OmniNode-ai/omnimarket",
    "OmniNode-ai/omnibase_core",
    "OmniNode-ai/omnibase_infra",
    "OmniNode-ai/omniintelligence",
    "OmniNode-ai/onex_change_control",
)


def _real_occ_yamlfmt() -> Path | None:
    """Resolve the CONSUMING repo's own ``.yamlfmt``, or None when absent.

    Mirrors ``test_occ_emitter_literal_pins_omn_15407._occ_yamlfmt_config`` and
    adds the canonical-registry location, because a per-ticket worktree does not
    sit beside its sibling clones.
    """
    candidates = [os.environ.get("OCC_REPO_DIR"), "../onex_change_control"]
    omni_home = os.environ.get("OMNI_HOME")
    if omni_home:
        candidates.append(str(Path(omni_home) / "onex_change_control"))
    for candidate in candidates:
        if not candidate:
            continue
        conf = Path(candidate).expanduser().resolve() / ".yamlfmt"
        if conf.is_file():
            return conf
    return None


def _yamlfmt_configs() -> list[pytest.param]:
    """Every yamlfmt config this property is asserted against.

    The vendored copy always runs (so the property has force in CI, where no OCC
    clone exists); the real one runs wherever it resolves, which is what makes a
    narrowing edit to ``onex_change_control/.yamlfmt`` turn this file RED.
    """
    params = [pytest.param(_VENDORED_OCC_YAMLFMT, id="vendored-occ-yamlfmt")]
    real = _real_occ_yamlfmt()
    if real is not None:
        params.append(pytest.param(real, id="real-occ-yamlfmt"))
    return params


def _max_line_length(conf: Path) -> int:
    """Read the wrap column from a yamlfmt config — never assumed by this file."""
    parsed = yaml.safe_load(conf.read_text(encoding="utf-8")) or {}
    width = ((parsed.get("formatter") or {}) or {}).get("max_line_length")
    assert isinstance(width, int), f"{conf} declares no formatter.max_line_length"
    return width


def _yamlfmt_or_skip() -> str:
    binary = shutil.which("yamlfmt")
    if binary is None:  # pragma: no cover - environment guard
        pytest.skip("yamlfmt binary not available (installed in the CI gate)")
    return binary


def _assert_fixpoint(content: str, conf: Path, tmp_path: Path, label: str) -> None:
    binary = _yamlfmt_or_skip()
    target = tmp_path / "contract.yaml"
    target.write_text(content, encoding="utf-8")
    subprocess.run([binary, "-conf", str(conf), str(target)], capture_output=True)
    after = target.read_text(encoding="utf-8")
    assert after == content, (
        f"real yamlfmt ({conf}) rewrote {label}; the companion's own Pre-commit "
        "reports `files were modified by this hook` and the companion cannot "
        "merge (OMN-18592):\n"
        + "".join(
            difflib.unified_diff(
                content.splitlines(True), after.splitlines(True), "before", "after"
            )
        )
    )


def _contract(repo: str, pr_number: int, ticket_id: str = "OMN-18592") -> str:
    return render_compute_companion_contract(
        ticket_id=ticket_id,
        repo=repo,
        pr_number=pr_number,
        evidence_id=f"dod-{ticket_id.lower()}-pr-{pr_number}",
    )


class TestTheDefectIsRealBeforeItIsFixed:
    """Non-vacuity: the inputs below are ones the OLD folded form really folds."""

    @pytest.mark.parametrize("conf", _yamlfmt_configs())
    @pytest.mark.parametrize(
        ("repo", "pr_number"),
        [(_LONGEST_REPO, _FIVE_DIGIT_PR), (_OMN_18669_REPO, _OMN_18669_PR)],
    )
    def test_the_old_folded_render_carries_a_space_past_the_wrap_column(
        self, repo: str, pr_number: int, conf: Path
    ) -> None:
        # The pre-fix template's own bytes: a ``>`` folded scalar whose single
        # content line sits at the two-space block indent.
        folded_line = f"  {compute_contract_summary(repo=repo, pr_number=pr_number)}"
        width = _max_line_length(conf)
        spaces_past_wrap = [
            column
            for column, char in enumerate(folded_line)
            if char == " " and column > width
        ]
        assert spaces_past_wrap, (
            f"{repo}#{pr_number} does not exercise the defect at width {width}; "
            "this test would be vacuous"
        )


class TestTheCompanionContractSurvivesTheConsumingRepoFormatter:
    """AC1 — byte-identical under the consuming repo's own yamlfmt, any input."""

    @pytest.mark.parametrize("conf", _yamlfmt_configs())
    def test_the_longest_repo_at_a_five_digit_pr_is_a_fixpoint(
        self, conf: Path, tmp_path: Path
    ) -> None:
        _assert_fixpoint(
            _contract(_LONGEST_REPO, _FIVE_DIGIT_PR),
            conf,
            tmp_path,
            f"the compute companion contract for {_LONGEST_REPO}#{_FIVE_DIGIT_PR}",
        )

    @pytest.mark.parametrize("conf", _yamlfmt_configs())
    def test_the_omn_18669_recurrence_is_a_fixpoint(
        self, conf: Path, tmp_path: Path
    ) -> None:
        """The exact OCC#10180 case, which was repaired by hand on 2026-09-18."""
        _assert_fixpoint(
            _contract(_OMN_18669_REPO, _OMN_18669_PR, ticket_id="OMN-18669"),
            conf,
            tmp_path,
            f"the compute companion contract for {_OMN_18669_REPO}#{_OMN_18669_PR}",
        )

    def test_the_summary_still_says_the_same_sentence(self) -> None:
        """A rendering-style change must move zero parsed content."""
        parsed = yaml.safe_load(_contract(_LONGEST_REPO, _FIVE_DIGIT_PR))
        assert parsed["summary"] == compute_contract_summary(
            repo=_LONGEST_REPO, pr_number=_FIVE_DIGIT_PR
        )

    def test_an_over_width_summary_renders_as_a_literal_block(self) -> None:
        """AC2 — the fold-proof mechanism is the one ``check_value`` already uses."""
        assert "summary: |-\n" in _contract(_LONGEST_REPO, _FIVE_DIGIT_PR)

    def test_the_compute_contract_no_longer_emits_a_folded_summary(self) -> None:
        """A ``>`` summary is the defect shape; it must be gone at every width."""
        for repo, pr_number in (
            (_LONGEST_REPO, _FIVE_DIGIT_PR),
            (_OMN_18669_REPO, _OMN_18669_PR),
            ("OmniNode-ai/omnidash", 1),
        ):
            assert "summary: >" not in _contract(repo, pr_number)


class TestNoBlastRadius:
    """A value that already fitted must keep the byte-identical quoted form."""

    def test_a_summary_that_fits_keeps_the_byte_identical_quoted_form(self) -> None:
        """The renderer only switches shape for a value that would actually fold.

        ``OmniNode-ai/omniweb`` at a one-digit PR is the widest input whose
        quoted render still fits, so it pins the branch that leaves bytes alone.
        """
        rendered = _contract("OmniNode-ai/omniweb", 1)
        assert (
            'summary: "OCC contract authored by node_occ_companion_compute '
            '(OMN-14285) for OmniNode-ai/omniweb PR #1."\n'
        ) in rendered

    @pytest.mark.parametrize("conf", _yamlfmt_configs())
    @pytest.mark.parametrize("repo", _REGISTRY_SLUGS)
    @pytest.mark.parametrize("pr_number", [1, 1000, 99999])
    def test_every_registry_slug_is_a_fixpoint_at_every_pr_width(
        self, repo: str, pr_number: int, conf: Path, tmp_path: Path
    ) -> None:
        """The property that actually matters, swept rather than sampled.

        yamlfmt folds at the first space PAST the wrap column, not at the column,
        so total length alone does not predict the branch: ``OmniNode-ai/omniweb``
        keeps the quoted form at 109 characters because its last space falls
        earlier, while ``OmniNode-ai/omnidash`` folds at 106. Sweeping the whole
        registry is what keeps that arithmetic out of this file.
        """
        _assert_fixpoint(
            _contract(repo, pr_number), conf, tmp_path, f"{repo}#{pr_number}"
        )

    def test_the_slugs_that_would_fold_take_the_literal_block_branch(self) -> None:
        """Both branches are exercised, so neither is dead code.

        A top-level ``summary:`` key costs eight more columns than the old
        two-space folded-block indent, so at realistic PR numbers every registry
        slug but ``omniweb`` renders ``summary: |-``. This change therefore moves
        the bytes of nearly every compute-path companion, not only the ones that
        were already failing — stated rather than implied.
        """
        folding = [slug for slug in _REGISTRY_SLUGS if slug != "OmniNode-ai/omniweb"]
        for repo in folding:
            assert "summary: |-\n" in _contract(repo, 1000), repo
        assert 'summary: "' in _contract("OmniNode-ai/omniweb", 1000)

    def test_the_born_path_summary_is_untouched(self) -> None:
        """AC4 — the OMN-13317 F1 quoted scalar naming no repo is unchanged."""
        rendered = render_companion_contract(
            ticket_id="OMN-18592",
            repo=_LONGEST_REPO,
            pr_number=_FIVE_DIGIT_PR,
            evidence_id="dod-omn-18592-pr-99999",
        )
        assert (
            'summary: "OCC Evidence-Source autobind companion (OMN-13317 F1) '
            'for PR #99999."\n'
        ) in rendered

    @pytest.mark.parametrize("conf", _yamlfmt_configs())
    def test_the_born_path_contract_is_still_a_fixpoint(
        self, conf: Path, tmp_path: Path
    ) -> None:
        rendered = render_companion_contract(
            ticket_id="OMN-18592",
            repo=_LONGEST_REPO,
            pr_number=_FIVE_DIGIT_PR,
            evidence_id="dod-omn-18592-pr-99999",
        )
        _assert_fixpoint(rendered, conf, tmp_path, "the born-path companion contract")


class TestTheVendoredConfigCannotGoStale:
    """AC5 — the copy CI runs against is pinned to the real consuming config."""

    def test_the_vendored_occ_yamlfmt_fixture_matches_the_real_one(self) -> None:
        real = _real_occ_yamlfmt()
        if real is None:
            pytest.skip("no onex_change_control clone resolvable (CI has none)")
        assert _VENDORED_OCC_YAMLFMT.read_text(encoding="utf-8") == real.read_text(
            encoding="utf-8"
        ), (
            f"{_VENDORED_OCC_YAMLFMT} has drifted from {real}; the CI half of "
            "this property is asserting a stale formatter configuration"
        )
